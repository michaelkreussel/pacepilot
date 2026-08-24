(() => {
  const chat = document.querySelector("[data-coach-chat]");
  if (!chat) return;

  const form = chat.querySelector("[data-coach-form]");
  const messages = chat.querySelector("[data-coach-messages]");
  const messageList = messages?.querySelector("[data-coach-message-list]");
  const textarea = form?.querySelector("textarea");
  const submit = form?.querySelector("button[type='submit']");
  if (!form || !messages || !messageList || !textarea || !submit) return;

  const conversationId = chat.dataset.conversationId;
  const deleteButton = document.querySelector(`[data-delete-chat="${CSS.escape(conversationId)}"]`);

  const scrollToBottom = () => {
    messages.scrollTop = messages.scrollHeight;
  };

  const createSvg = (pathData) => {
    const namespace = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(namespace, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.classList.add("size-3", "fill-none", "stroke-current", "stroke-2", "transition-transform", "group-open:rotate-180");
    const path = document.createElementNS(namespace, "path");
    path.setAttribute("d", pathData);
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.append(path);
    return svg;
  };

  const createUserMessage = (text) => {
    const article = document.createElement("article");
    article.className = "flex justify-end";
    const bubble = document.createElement("div");
    bubble.className = "max-w-[88%] rounded-xl rounded-br-sm border border-primary/20 bg-primary-subtle px-3 py-2 text-sm leading-6 text-primary-emphasis sm:max-w-[78%]";
    bubble.textContent = text;
    article.append(bubble);
    return article;
  };

  const createActivityStep = (labelText, { id, summary, state } = {}) => {
    const item = document.createElement("li");
    item.className = "relative pl-3";
    if (id !== undefined) item.dataset.toolCall = id;
    if (state) item.dataset.toolState = state;

    const dot = document.createElement("span");
    dot.className = `absolute -left-[0.9375rem] top-1 size-1.5 rounded-full ${state === "running" ? "bg-primary" : "bg-muted-foreground"}`;
    dot.dataset.toolDot = "";

    const row = document.createElement("div");
    row.className = "flex flex-wrap items-baseline gap-x-2 gap-y-0.5";
    const label = document.createElement("span");
    label.className = "font-medium text-foreground";
    label.dataset.toolLabel = "";
    label.textContent = labelText;
    row.append(label);

    if (state) {
      const status = document.createElement("span");
      status.className = "text-[0.6875rem]";
      status.dataset.toolStatus = "";
      status.textContent = "Wird ausgeführt";
      row.append(status);
    }

    item.append(dot, row);
    if (summary) {
      const detail = document.createElement("p");
      detail.className = "mt-0.5 leading-5";
      detail.dataset.toolSummary = "";
      detail.textContent = summary;
      item.append(detail);
    }
    return item;
  };

  const createAssistantMessage = () => {
    const article = document.createElement("article");

    const row = document.createElement("div");
    row.className = "flex items-start gap-2.5";
    const avatar = document.createElement("div");
    avatar.className = "grid size-7 shrink-0 place-items-center rounded-lg bg-secondary font-display text-[0.6875rem] font-bold text-secondary-foreground";
    avatar.textContent = "P";

    const body = document.createElement("div");
    body.className = "min-w-0 flex-1";
    const activity = document.createElement("details");
    activity.className = "group mb-2 text-xs text-muted-foreground";
    activity.dataset.coachActivity = "";
    const activityHeader = document.createElement("summary");
    activityHeader.className = "flex w-fit cursor-pointer list-none items-center gap-1.5 rounded-md py-0.5 font-medium transition hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring";
    const activityIcon = document.createElement("span");
    activityIcon.className = "relative flex size-3.5 items-center justify-center";
    const activityDot = document.createElement("span");
    activityDot.className = "coach-activity-pulse size-1.5 rounded-full bg-primary";
    activityIcon.append(activityDot);
    const activitySummary = document.createElement("span");
    activitySummary.className = "coach-activity-wave";
    activitySummary.dataset.activitySummary = "";
    activitySummary.textContent = "Deine Frage wird analysiert";
    activityHeader.append(activityIcon, activitySummary, createSvg("m6 9 6 6 6-6"));

    const activityLog = document.createElement("ol");
    activityLog.className = "mt-1.5 space-y-1.5 border-l border-border pl-3";
    activityLog.dataset.activityLog = "";
    const initialStep = createActivityStep("Frage analysiert");
    initialStep.dataset.activityStep = "";
    activityLog.append(initialStep);
    activity.append(activityHeader, activityLog);

    const answer = document.createElement("div");
    answer.className = "whitespace-pre-wrap text-sm leading-6 text-foreground";
    answer.dataset.answerText = "";

    const artifacts = document.createElement("div");
    artifacts.dataset.proposalArtifacts = "";

    body.append(activity, answer, artifacts);
    row.append(avatar, body);
    article.append(row);
    return {
      activity,
      activityDot,
      activityLog,
      activitySummary,
      answer,
      artifacts,
      answerStarted: false,
      article,
      body,
      startedAt: performance.now(),
      toolCount: 0,
    };
  };

  const setActivityStatus = (assistant, label, running = true) => {
    assistant.activitySummary.textContent = label;
    assistant.activitySummary.classList.toggle("coach-activity-wave", running);
    assistant.activityDot.className = running
      ? "coach-activity-pulse size-1.5 rounded-full bg-primary"
      : "size-1.5 rounded-full bg-muted-foreground";
  };

  const formatElapsed = (milliseconds) => {
    const seconds = Math.max(1, Math.round(milliseconds / 1000));
    if (seconds < 60) return `${seconds} Sek.`;
    return `${Math.floor(seconds / 60)} Min. ${seconds % 60} Sek.`;
  };

  const addTool = (assistant, data) => {
    let item = assistant.activityLog.querySelector(`[data-tool-call="${CSS.escape(data.id)}"]`);
    if (!item) {
      item = createActivityStep(data.label, { id: data.id, summary: data.summary, state: "running" });
      item.dataset.startedAt = performance.now().toString();
      assistant.activityLog.append(item);
      assistant.toolCount += 1;
    }
    return item;
  };

  const completeTool = (assistant, data, failed) => {
    const item = addTool(assistant, data);
    item.dataset.toolState = failed ? "failed" : "completed";
    const duration = performance.now() - Number(item.dataset.startedAt || performance.now());
    const status = item.querySelector("[data-tool-status]");
    if (status) status.textContent = `${failed ? "Fehlgeschlagen" : "Abgeschlossen"} · ${formatElapsed(duration)}`;
    const dot = item.querySelector("[data-tool-dot]");
    if (dot) dot.className = `absolute -left-[0.9375rem] top-1 size-1.5 rounded-full ${failed ? "bg-danger" : "bg-muted-foreground"}`;
  };

  const currentToolLabel = (assistant) => {
    const running = assistant.activityLog.querySelectorAll('[data-tool-state="running"] [data-tool-label]');
    return running.length ? running[running.length - 1].textContent : null;
  };

  const finishActivity = (assistant) => {
    if (!assistant.activityLog.querySelector("[data-activity-final]")) {
      const finalStep = createActivityStep("Antwort formuliert");
      finalStep.dataset.activityFinal = "";
      assistant.activityLog.append(finalStep);
    }
    const steps = assistant.toolCount + 2;
    setActivityStatus(assistant, `${steps} Schritte · ${formatElapsed(performance.now() - assistant.startedAt)} nachgedacht`, false);
  };

  const failActivity = (assistant, message) => {
    setActivityStatus(assistant, "Antwort nicht abgeschlossen", false);
    assistant.activityDot.className = "size-1.5 rounded-full bg-danger";
    let error = assistant.body.querySelector("[data-coach-error]");
    if (!error) {
      error = document.createElement("p");
      error.className = "mt-2 text-xs text-danger-emphasis";
      error.dataset.coachError = "";
      error.setAttribute("role", "status");
      assistant.body.append(error);
    }
    error.textContent = message;
  };

  const addProposal = async (assistant, data) => {
    if (assistant.artifacts.querySelector(`[data-workout-id="${CSS.escape(String(data.workout_id))}"]`)) return;
    if (typeof data.card_url !== "string" || !data.card_url.startsWith(`/coach/${conversationId}/runs/`)) return;
    try {
      const response = await fetch(data.card_url, { headers: { Accept: "text/html" } });
      if (!response.ok || !response.headers.get("content-type")?.includes("text/html")) throw new Error();
      const documentFragment = new DOMParser().parseFromString(await response.text(), "text/html");
      const card = documentFragment.body.firstElementChild;
      if (!card?.matches("[data-proposal-card]") || card.dataset.workoutId !== String(data.workout_id)) throw new Error();
      assistant.artifacts.append(document.importNode(card, true));
    } catch (_) {
      const notice = document.createElement("p");
      notice.className = "mt-2 text-xs text-warning-emphasis";
      notice.setAttribute("role", "status");
      notice.textContent = "Der Vorschlag wurde gespeichert. Lade den Chat neu, um die Karte zu öffnen.";
      assistant.artifacts.append(notice);
    }
  };

  const handleEvent = async (name, data, assistant) => {
    if (name === "status") {
      setActivityStatus(assistant, currentToolLabel(assistant) || data.label);
    } else if (name === "tool.started") {
      addTool(assistant, data);
      setActivityStatus(assistant, data.label);
    } else if (name === "tool.completed" || name === "tool.failed") {
      completeTool(assistant, data, name === "tool.failed");
      setActivityStatus(assistant, currentToolLabel(assistant) || "Erkenntnisse werden eingeordnet");
    } else if (name === "answer.delta") {
      if (!assistant.answerStarted) {
        assistant.answerStarted = true;
        setActivityStatus(assistant, "Antwort wird formuliert");
      }
      assistant.answer.append(document.createTextNode(data.text));
    } else if (name === "answer.completed") {
      finishActivity(assistant);
    } else if (name === "proposal.created") {
      await addProposal(assistant, data);
    } else if (name === "error") {
      failActivity(assistant, data.message);
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
    if (response.redirected || !response.headers.get("content-type")?.includes("text/event-stream")) {
      throw new Error("Der Coach hat keine gültige Streaming-Antwort geliefert.");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let terminalEventReceived = false;
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
        if (dataLines.length) {
          if (["answer.completed", "error"].includes(eventName)) terminalEventReceived = true;
           await handleEvent(eventName, JSON.parse(dataLines.join("\n")), assistant);
        }
      }
      if (done) break;
    }
    if (!terminalEventReceived) {
      throw new Error("Die Streaming-Antwort wurde vorzeitig beendet.");
    }
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = textarea.value.trim();
    if (!text || submit.disabled) return;

    messageList.querySelector("[data-coach-empty]")?.remove();
    messageList.append(createUserMessage(text));
    const assistant = createAssistantMessage();
    messageList.append(assistant.article);
    textarea.value = "";
    textarea.style.height = "auto";
    submit.disabled = true;
    if (deleteButton) deleteButton.disabled = true;

    const selectedTitle = document.querySelector("[data-selected-conversation-title]");
    const listTitle = document.querySelector(`[data-conversation-title="${CSS.escape(conversationId)}"]`);
    if (selectedTitle?.textContent.trim() === "Neuer Chat") {
      const title = `${text.slice(0, 157)}${text.length > 157 ? "..." : ""}`;
      selectedTitle.textContent = title;
      if (listTitle) listTitle.textContent = title;
    }
    scrollToBottom();

    const body = new FormData();
    body.set("message", text);
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body,
        headers: {
          Accept: "text/event-stream",
          "X-CSRF-Token": form.elements.namedItem("_csrf_token").value,
        },
      });
      await consumeEvents(response, assistant);
    } catch (error) {
      failActivity(assistant, error.message);
    } finally {
      submit.disabled = false;
      if (deleteButton) deleteButton.disabled = false;
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
