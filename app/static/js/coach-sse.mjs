const TERMINAL_EVENTS = new Set(["answer.completed", "error"]);

function parseBlock(block) {
  let eventName = "message";
  const dataLines = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    if (line.startsWith("data:")) {
      const value = line.slice(5);
      dataLines.push(value.startsWith(" ") ? value.slice(1) : value);
    }
  }
  if (!dataLines.length) return null;

  try {
    return [eventName, JSON.parse(dataLines.join("\n"))];
  } catch (_) {
    throw new Error("Die Streaming-Antwort enthält ungültige Daten.");
  }
}

export async function consumeSse(body, handleEvent) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const dispatch = async (block) => {
    const event = parseBlock(block);
    if (!event) return false;
    const [eventName, data] = event;
    await handleEvent(eventName, data);
    return TERMINAL_EVENTS.has(eventName);
  };

  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });

      let boundary = buffer.search(/\r?\n\r?\n/);
      while (boundary !== -1) {
        const separator = buffer.match(/\r?\n\r?\n/)[0];
        if (await dispatch(buffer.slice(0, boundary))) {
          await reader.cancel();
          return;
        }
        buffer = buffer.slice(boundary + separator.length);
        boundary = buffer.search(/\r?\n\r?\n/);
      }
      if (done) break;
    }

    if (buffer.trim() && (await dispatch(buffer))) return;
    throw new Error("Die Streaming-Antwort wurde vorzeitig beendet.");
  } catch (error) {
    try {
      await reader.cancel();
    } catch (_) {
      // Preserve the parser or handler error.
    }
    throw error;
  }
}
