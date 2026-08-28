import assert from "node:assert/strict";
import test from "node:test";

import { consumeSse } from "../app/static/js/coach-sse.mjs";

const encoder = new TextEncoder();

function bodyFromChunks(chunks) {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  });
}

test("decodes UTF-8 characters split across chunks", async () => {
  const encoded = encoder.encode(
    'event: answer.delta\ndata: {"text":"müde"}\n\nevent: answer.completed\ndata: {"html":"<article></article>"}\n\n',
  );
  const umlaut = encoded.indexOf(0xc3);
  const events = [];

  await consumeSse(bodyFromChunks([encoded.slice(0, umlaut + 1), encoded.slice(umlaut + 1)]), (name, data) => {
    events.push([name, data]);
  });

  assert.deepEqual(events[0], ["answer.delta", { text: "müde" }]);
  assert.equal(events.at(-1)[0], "answer.completed");
});

test("joins multiline data fields and accepts CRLF framing", async () => {
  const events = [];
  const source =
    'event: answer.delta\r\ndata: {"text":\r\ndata: "Zeile"}\r\n\r\n' +
    'event: error\r\ndata: {"html":"<article></article>"}\r\n\r\n';

  await consumeSse(bodyFromChunks([encoder.encode(source)]), (name, data) => {
    events.push([name, data]);
  });

  assert.deepEqual(events, [
    ["answer.delta", { text: "Zeile" }],
    ["error", { html: "<article></article>" }],
  ]);
});

test("rejects malformed JSON", async () => {
  const body = bodyFromChunks([encoder.encode("event: error\ndata: not-json\n\n")]);

  await assert.rejects(
    consumeSse(body, () => {}),
    /Die Streaming-Antwort enthält ungültige Daten\./,
  );
});

test("cancels an open stream after malformed JSON", async () => {
  let cancelled = false;
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode("event: answer.delta\ndata: not-json\n\n"));
    },
    cancel() {
      cancelled = true;
    },
  });

  await assert.rejects(consumeSse(body, () => {}));

  assert.equal(cancelled, true);
});

test("rejects a stream without a terminal event", async () => {
  const body = bodyFromChunks([
    encoder.encode('event: answer.delta\ndata: {"text":"unvollständig"}\n\n'),
  ]);

  await assert.rejects(
    consumeSse(body, () => {}),
    /Die Streaming-Antwort wurde vorzeitig beendet\./,
  );
});

test("accepts completed and failed terminal events", async () => {
  for (const terminalEvent of ["answer.completed", "error"]) {
    const body = bodyFromChunks([
      encoder.encode(`event: ${terminalEvent}\ndata: {"html":"<article></article>"}\n\n`),
    ]);

    await consumeSse(body, () => {});
  }
});

test("stops consuming events after a terminal event", async () => {
  const events = [];
  const body = bodyFromChunks([
    encoder.encode(
      'event: answer.completed\ndata: {"html":"<article></article>"}\n\n' +
        "event: answer.delta\ndata: not-json\n\n",
    ),
  ]);

  await consumeSse(body, (name) => events.push(name));

  assert.deepEqual(events, ["answer.completed"]);
});
