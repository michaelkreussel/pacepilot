(() => {
  const chat = document.getElementById("coach-chat");
  const form = document.getElementById("coach-form");
  if (!chat || !form) return;

  const conversation = document.getElementById("coach-conversation");
  const welcome = document.getElementById("coach-welcome");
  const message = document.getElementById("coach-message");
  const submit = document.getElementById("coach-submit");
  const userTemplate = document.getElementById("coach-user-template");
  const assistantTemplate = document.getElementById("coach-assistant-template");
  const history = [];

  const scrollToLatest = () => {
    conversation.scrollTop = conversation.scrollHeight;
  };

  const formatTime = (seconds) => {
    const minutes = Math.floor(seconds / 60);
    return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
  };

  const appendUserMessage = (content) => {
    const fragment = userTemplate.content.cloneNode(true);
    fragment.querySelector("[data-message-content]").textContent = content;
    conversation.append(fragment);
  };

  const createAssistantRun = () => {
    const fragment = assistantTemplate.content.cloneNode(true);
    const root = fragment.querySelector("[data-chat-message]");
    conversation.append(fragment);
    const run = {
      root,
      status: root.querySelector("[data-run-status]"),
      spinner: root.querySelector("[data-run-spinner]"),
      timer: root.querySelector("[data-run-timer]"),
      currentStep: root.querySelector("[data-current-step]"),
      currentKicker: root.querySelector("[data-current-kicker]"),
      currentState: root.querySelector("[data-current-state]"),
      currentLabel: root.querySelector("[data-current-label]"),
      waiting: root.querySelector("[data-waiting-explanation]"),
      progress: root.querySelector("[data-progress-list]"),
      answerWrap: root.querySelector("[data-answer-wrap]"),
      answer: root.querySelector("[data-answer]"),
      startedAt: performance.now(),
      interval: null,
      complete: false,
      successful: false,
      runId: null,
    };
    run.interval = window.setInterval(() => {
      run.timer.textContent = formatTime(Math.floor((performance.now() - run.startedAt) / 1000));
    }, 250);
    const toggle = root.querySelector("[data-activity-toggle]");
    const details = root.querySelector("[data-activity-details]");
    const toggleLabel = root.querySelector("[data-activity-toggle-label]");
    toggle.addEventListener("click", () => {
      const expanded = toggle.getAttribute("aria-expanded") === "true";
      toggle.setAttribute("aria-expanded", String(!expanded));
      details.classList.toggle("hidden", expanded);
      toggleLabel.textContent = expanded ? "Details anzeigen" : "Details ausblenden";
    });
    return run;
  };

  const setCurrentActivity = (run, label, description) => {
    run.currentStep.classList.remove("is-complete", "is-error");
    run.currentKicker.textContent = "Aktueller Schritt";
    run.currentState.textContent = "läuft";
    run.currentLabel.textContent = label;
    run.waiting.textContent = description;
  };

  const addProgress = (run, event) => {
    const item = document.createElement("li");
    item.className = "coach-progress-item is-running text-xs leading-5";
    item.dataset.eventType = event.type;
    item.dataset.label = event.label || (event.type === "analysis_update" ? "Auswertung" : "Coach");
    item.dataset.description = event.content;
    if (event.tool) item.dataset.tool = event.tool;
    if (event.type === "tool_started") item.dataset.startedAt = String(performance.now());

    const mark = document.createElement("span");
    mark.className = "coach-progress-mark";
    mark.dataset.progressMark = "";
    mark.textContent = "✓";
    const body = document.createElement("div");
    body.className = "min-w-0 flex-1";
    const heading = document.createElement("div");
    heading.className = "flex items-start justify-between gap-3";
    const text = document.createElement("p");
    const label = document.createElement("strong");
    label.className = "text-foreground";
    label.textContent = event.label || (event.type === "analysis_update" ? "Auswertung" : "Coach");
    text.append(label, document.createTextNode(` · ${event.content}`));
    const duration = document.createElement("span");
    duration.className = "shrink-0 font-mono text-[0.625rem] tabular-nums";
    duration.dataset.toolDuration = "";
    heading.append(text, duration);
    body.append(heading);
    item.append(mark, body);
    run.progress.append(item);
    return item;
  };

  const completeProgressItem = (item, event, failed = false) => {
    item.classList.remove("is-running");
    item.classList.add(failed ? "is-error" : "is-complete");
    item.dataset.eventType = failed ? "tool_error" : "completed";
    item.querySelector("[data-progress-mark]").textContent = failed ? "!" : "✓";
    const startedAt = Number(item.dataset.startedAt);
    if (Number.isFinite(startedAt)) {
      item.querySelector("[data-tool-duration]").textContent = `${((performance.now() - startedAt) / 1000).toFixed(1)} s`;
    }
    if (event?.content) {
      const summary = document.createElement("p");
      summary.className = "mt-1 text-foreground";
      summary.textContent = event.content;
      item.querySelector("div").append(summary);
    }
  };

  const completePhase = (run, eventType) => {
    const items = [...run.progress.querySelectorAll(".is-running")];
    const item = items.reverse().find((candidate) => candidate.dataset.eventType === eventType);
    if (item) completeProgressItem(item);
  };

  const showRunningToolOr = (run, label, description) => {
    const running = [...run.progress.querySelectorAll(".is-running[data-tool]")].pop();
    if (running) {
      setCurrentActivity(run, running.dataset.label, running.dataset.description);
      return;
    }
    setCurrentActivity(run, label, description);
  };

  const completeToolProgress = (run, event, failed = false) => {
    const matches = [...run.progress.querySelectorAll(`[data-tool="${event.tool}"]`)].reverse();
    const item = matches.find((candidate) => candidate.dataset.eventType === "tool_started");
    if (item) {
      completeProgressItem(item, event, failed);
      return;
    }
    completeProgressItem(addProgress(run, event), event, failed);
  };

  const finishRun = (run, successful) => {
    run.complete = true;
    run.successful = successful;
    window.clearInterval(run.interval);
    run.timer.textContent = formatTime(Math.floor((performance.now() - run.startedAt) / 1000));
    run.spinner.className = `size-3 shrink-0 rounded-full ${successful ? "bg-success" : "bg-danger"}`;
    run.status.textContent = successful ? "Analyse abgeschlossen" : "Analyse abgebrochen";
    [...run.progress.querySelectorAll(".is-running")].forEach((item) => completeProgressItem(item));
    run.currentStep.classList.add(successful ? "is-complete" : "is-error");
    run.currentKicker.textContent = successful ? "Abgeschlossen" : "Abgebrochen";
    run.currentState.textContent = successful ? "fertig" : "Fehler";
    run.currentLabel.textContent = successful ? "Antwort bereit" : "Analyse nicht abgeschlossen";
    run.waiting.textContent = successful
      ? "Die verwendeten Schritte sind unten als erledigt markiert."
      : "Die Anfrage konnte nicht vollständig abgeschlossen werden.";
  };

  const handleEvent = (run, event) => {
    if (event.run_id) run.runId = event.run_id;
    console.debug("[PacePilot Coach] stream event", {
      runId: run.runId,
      type: event.type,
      phase: event.phase,
      tool: event.tool,
      elapsedSeconds: event.elapsed_seconds,
    });
    if (event.elapsed_seconds !== null && event.elapsed_seconds !== undefined) {
      run.timer.textContent = formatTime(event.elapsed_seconds);
    }
    if (event.type === "waiting") {
      run.status.textContent = event.label ? `${event.label} läuft ...` : "Coach arbeitet ...";
      setCurrentActivity(run, event.label || run.currentLabel.textContent, event.content);
    } else if (event.type === "status") {
      run.status.textContent = "Passende Daten werden ausgewählt ...";
      addProgress(run, event);
      setCurrentActivity(run, "Frage verstehen", event.content);
    } else if (event.type === "tool_started") {
      completePhase(run, "status");
      run.status.textContent = `${event.label || "Datenabfrage"} wird geladen ...`;
      addProgress(run, event);
      setCurrentActivity(run, event.label || "Datenabfrage", event.content);
    } else if (event.type === "tool_result_summary") {
      run.status.textContent = "Ergebnis wird eingeordnet ...";
      completeToolProgress(run, event);
      showRunningToolOr(
        run,
        "Ergebnisse einordnen",
        "Der Coach prüft, ob ein weiterer Vergleich nötig ist.",
      );
    } else if (event.type === "tool_error") {
      run.status.textContent = `${event.label || "Datenabfrage"} fehlgeschlagen`;
      completeToolProgress(run, event, true);
      setCurrentActivity(run, "Alternative Daten prüfen", event.content);
      console.warn("[PacePilot Coach] tool failed", { runId: run.runId, tool: event.tool });
    } else if (event.type === "analysis_update") {
      run.status.textContent = "Zusammenhänge werden formuliert ...";
      addProgress(run, event);
      setCurrentActivity(run, "Antwort vorbereiten", event.content);
    } else if (event.type === "final_response") {
      run.answerWrap.classList.remove("hidden");
      if (event.content) {
        completePhase(run, "analysis_update");
        run.status.textContent = "Antwort wird live übertragen ...";
        setCurrentActivity(
          run,
          "Antwort schreiben",
          "Die Einordnung ist fertig und wird jetzt Stück für Stück übertragen.",
        );
        if (event.replace) run.answer.textContent = event.content;
        else run.answer.textContent += event.content;
      }
      if (event.done) finishRun(run, true);
    } else if (event.type === "error") {
      run.answerWrap.classList.remove("hidden");
      run.answer.textContent = run.runId
        ? `${event.content}\n\nRun-ID: ${run.runId}`
        : event.content;
      finishRun(run, false);
      console.error("[PacePilot Coach] server error", { runId: run.runId, message: event.content });
    }
    scrollToLatest();
  };

  document.querySelectorAll("[data-coach-question]").forEach((button) => {
    button.addEventListener("click", () => {
      message.value = button.dataset.coachQuestion;
      message.focus();
    });
  });

  message.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  form.addEventListener("submit", async (formEvent) => {
    formEvent.preventDefault();
    const question = message.value.trim();
    if (!question || submit.disabled || chat.dataset.configured !== "true") return;

    welcome?.remove();
    appendUserMessage(question);
    const run = createAssistantRun();
    message.value = "";
    submit.disabled = true;
    scrollToLatest();

    try {
      const response = await fetch("/coach/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/x-ndjson" },
        body: JSON.stringify({ message: question, history: history.slice(-6) }),
      });
      if (!response.ok) {
        const payload = await response.json();
        throw new Error(payload.detail || "Der Coach konnte nicht gestartet werden.");
      }
      if (!response.body) throw new Error("Der Browser unterstützt den Antwortstream nicht.");
      run.runId = response.headers.get("X-Coach-Run-ID");
      console.info("[PacePilot Coach] run started", { runId: run.runId });

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        lines.filter(Boolean).forEach((line) => {
          try {
            handleEvent(run, JSON.parse(line));
          } catch (error) {
            console.error("[PacePilot Coach] invalid stream event", {
              runId: run.runId,
              lineLength: line.length,
              error,
            });
            throw error;
          }
        });
        if (done) break;
      }
      if (buffer.trim()) handleEvent(run, JSON.parse(buffer));
      if (run.successful && run.answer.textContent) {
        history.push(
          { role: "user", content: question },
          { role: "assistant", content: run.answer.textContent },
        );
        if (history.length > 6) history.splice(0, history.length - 6);
      }
    } catch (error) {
      console.error("[PacePilot Coach] request failed", { runId: run.runId, error });
      run.answerWrap.classList.remove("hidden");
      run.answer.textContent = error instanceof Error
        ? error.message
        : "Der Coach ist gerade nicht erreichbar.";
      finishRun(run, false);
    } finally {
      if (!run.complete) finishRun(run, Boolean(run.answer.textContent));
      submit.disabled = false;
      message.focus();
      scrollToLatest();
    }
  });
})();
