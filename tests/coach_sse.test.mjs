import assert from "node:assert/strict";
import test from "node:test";

import { addWorkoutArtifact } from "../app/static/js/coach-artifacts.mjs";
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
    'event: answer.failed\r\ndata: {"html":"<article></article>"}\r\n\r\n';

  await consumeSse(bodyFromChunks([encoder.encode(source)]), (name, data) => {
    events.push([name, data]);
  });

  assert.deepEqual(events, [
    ["answer.delta", { text: "Zeile" }],
    ["answer.failed", { html: "<article></article>" }],
  ]);
});

test("rejects malformed JSON", async () => {
  const body = bodyFromChunks([encoder.encode("event: answer.failed\ndata: not-json\n\n")]);

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
  for (const terminalEvent of ["answer.completed", "answer.failed"]) {
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

test("ignores events outside the browser presentation contract", async () => {
  const events = [];
  const body = bodyFromChunks([
    encoder.encode(
      'event: debug.trace\ndata: {"detail":"ignored"}\n\n' +
        'event: artifact.available\ndata: {"workout_id":1}\n\n' +
        'event: answer.completed\ndata: {"html":"<article></article>"}\n\n',
    ),
  ]);

  await consumeSse(body, (name) => events.push(name));

  assert.deepEqual(events, ["artifact.available", "answer.completed"]);
});

test("renders each workout artifact once and shows a reload notice on failure", async (t) => {
  const originalCss = globalThis.CSS;
  const originalDocument = globalThis.document;
  const originalDomParser = globalThis.DOMParser;
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.CSS = originalCss;
    globalThis.document = originalDocument;
    globalThis.DOMParser = originalDomParser;
    globalThis.fetch = originalFetch;
  });

  class FakeElement {
    constructor(workoutId = undefined) {
      this.children = [];
      this.dataset = workoutId === undefined ? {} : { workoutId: String(workoutId) };
    }

    matches(selector) {
      return selector === "[data-proposal-card]";
    }

    querySelector(selector) {
      const workoutId = selector.match(/data-workout-id="([^"]+)"/)?.[1];
      return this.children.find((child) => child.dataset.workoutId === workoutId) ?? null;
    }

    append(child) {
      this.children.push(child);
    }

    setAttribute(name, value) {
      this[name] = value;
    }
  }

  globalThis.CSS = { escape: String };
  globalThis.document = {
    createElement: () => new FakeElement(),
    importNode: (element) => element,
  };
  globalThis.DOMParser = class {
    parseFromString(html) {
      return {
        body: {
          firstElementChild: new FakeElement(html.match(/data-workout-id="([^"]+)"/)?.[1]),
        },
      };
    }
  };

  const artifacts = new FakeElement();
  let fetches = 0;
  globalThis.fetch = async () => {
    fetches += 1;
    return {
      ok: true,
      headers: { get: () => "text/html; charset=utf-8" },
      text: async () => '<article data-proposal-card data-workout-id="7"></article>',
    };
  };
  const artifact = {
    workout_id: 7,
    card_url: "/coach/3/messages/5/proposal-card",
  };

  await addWorkoutArtifact(artifacts, "3", artifact);
  await addWorkoutArtifact(artifacts, "3", artifact);

  assert.equal(fetches, 1);
  assert.equal(artifacts.children.length, 1);
  assert.equal(artifacts.children[0].dataset.workoutId, "7");

  globalThis.fetch = async () => ({ ok: false, headers: { get: () => null } });
  await addWorkoutArtifact(artifacts, "3", {
    workout_id: 8,
    card_url: "/coach/3/messages/5/proposal-card",
  });

  const notice = artifacts.children.at(-1);
  assert.equal(notice.role, "status");
  assert.match(notice.textContent, /Lade den Chat neu/);
});
