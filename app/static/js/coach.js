(() => {
  const chat = document.querySelector("[data-coach-chat]");
  if (!chat) return;

  const form = chat.querySelector("[data-coach-form]");
  const messages = chat.querySelector("[data-coach-messages]");
  const textarea = form?.querySelector("textarea");
  const submit = form?.querySelector("button[type='submit']");
  if (!form || !messages || !textarea || !submit) return;

  const scrollToBottom = () => {
    messages.scrollTop = messages.scrollHeight;
  };

  const createUserMessage = (text) => {
    const article = document.createElement("article");
    article.className = "ml-auto max-w-[85%] rounded-2xl rounded-br-md bg-primary px-4 py-3 text-sm leading-6 text-primary-foreground sm:max-w-[70%]";
    article.textContent = text;
    return article;
  };

  const createAssistantMessage = () => {
    const article = document.createElement("article");
    article.className = "max-w-3xl";

    const row = document.createElement("div");
    row.className = "flex items-start gap-3";
    const avatar = document.createElement("div");
    avatar.className = "grid size-8 shrink-0 place-items-center rounded-lg bg-secondary font-display text-xs font-bold text-secondary-foreground";
    avatar.textContent = "P";

    const body = document.createElement("div");
    body.className = "min-w-0 flex-1";
    const answer = document.createElement("div");
    answer.className = "whitespace-pre-wrap text-sm leading-7 text-foreground";
    answer.dataset.answerText = "";
    const status = document.createElement("div");
    status.className = "mt-3 flex items-center gap-2 text-xs text-muted-foreground transition-opacity";
    status.dataset.coachStatus = "";
    const pulse = document.createElement("span");
    pulse.className = "size-1.5 animate-pulse rounded-full bg-primary";
    const statusText = document.createElement("span");
    statusText.textContent = "Deine Frage wird analysiert";
    status.append(pulse, statusText);

    const tools = document.createElement("div");
    tools.className = "mt-3 space-y-2";
    tools.dataset.toolList = "";
    body.append(answer, status, tools);
    row.append(avatar, body);
    article.append(row);
    return { article, answer, status, statusText, tools };
  };

  const addTool = (container, data) => {
    let item = container.querySelector(`[data-tool-call="${CSS.escape(data.id)}"]`);
    if (!item) {
      item = document.createElement("details");
      item.className = "group rounded-lg border border-border bg-surface-muted/60 px-3 py-2 text-xs";
      item.dataset.toolCall = data.id;
      const summary = document.createElement("summary");
      summary.className = "cursor-pointer list-none font-medium text-muted-foreground";
      const label = document.createElement("span");
      label.textContent = data.label;
      const state = document.createElement("span");
      state.className = "ml-2 text-[0.6875rem] opacity-70";
      state.dataset.toolState = "";
      state.textContent = "Wird geladen";
      summary.append(label, state);
      item.append(summary);
      if (data.summary) {
        const detail = document.createElement("p");
        detail.className = "mt-2 text-muted-foreground";
        detail.textContent = data.summary;
        item.append(detail);
      }
      container.append(item);
    }
    return item;
  };

  const handleEvent = (name, data, assistant) => {
    if (name === "status") {
      assistant.statusText.textContent = data.label;
    } else if (name === "tool.started") {
      addTool(assistant.tools, data);
    } else if (name === "tool.completed" || name === "tool.failed") {
      const item = addTool(assistant.tools, data);
      const state = item.querySelector("[data-tool-state]");
      if (state) state.textContent = name === "tool.completed" ? "Abgeschlossen" : "Fehlgeschlagen";
    } else if (name === "answer.delta") {
      assistant.answer.append(document.createTextNode(data.text));
    } else if (name === "answer.completed") {
      assistant.status.remove();
    } else if (name === "error") {
      assistant.status.className = "mt-3 text-xs text-danger";
      assistant.status.replaceChildren(document.createTextNode(data.message));
    }
    scrollToBottom();
  };

  const consumeEvents = async (response, assistant) => {
    if (!response.ok || !response.body) {
      let message = "Der Coach ist gerade nicht erreichbar.";
      try {
        const error = await response.json();
        if (error.detail) message = error.detail;
      } catch (_) {
        // Keep the safe fallback message.
      }
      throw new Error(message);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() || "";
      for (const block of blocks) {
        let eventName = "message";
        const dataLines = [];
        for (const line of block.split("\n")) {
          if (line.startsWith("event:")) eventName = line.slice(6).trim();
          if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
        }
        if (dataLines.length) handleEvent(eventName, JSON.parse(dataLines.join("\n")), assistant);
      }
      if (done) break;
    }
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = textarea.value.trim();
    if (!text || submit.disabled) return;

    messages.querySelector("[data-coach-empty]")?.remove();
    messages.append(createUserMessage(text));
    const assistant = createAssistantMessage();
    messages.append(assistant.article);
    textarea.value = "";
    textarea.style.height = "auto";
    submit.disabled = true;
    scrollToBottom();

    const body = new FormData();
    body.set("message", text);
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body,
        headers: { Accept: "text/event-stream" },
      });
      await consumeEvents(response, assistant);
    } catch (error) {
      assistant.status.className = "mt-3 text-xs text-danger";
      assistant.status.replaceChildren(document.createTextNode(error.message));
    } finally {
      submit.disabled = false;
      textarea.focus();
    }
  });

  textarea.addEventListener("input", () => {
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 160)}px`;
  });
  textarea.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  scrollToBottom();
})();
