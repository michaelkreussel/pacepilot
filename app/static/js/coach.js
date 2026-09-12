import { addWorkoutArtifact, parseElement } from "./coach-artifacts.mjs?v=20260910-1";
import { consumeSse } from "./coach-sse.mjs?v=20260906-1";

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
  const selectedTitle = document.querySelector("[data-selected-conversation-title]");
  const listTitle = document.querySelector(`[data-conversation-title="${CSS.escape(conversationId)}"]`);
  const live = { assistant: null, failureHtml: null };

  const scrollToBottom = () => {
    messages.scrollTop = messages.scrollHeight;
  };

  const bindAssistant = (article) => {
    const answer = article.querySelector("[data-answer-text]");
    const artifacts = article.querySelector("[data-proposal-artifacts]");
    if (!answer || !artifacts) throw new Error("Ungültige Nachrichtendarstellung.");
    return { answer, article, artifacts };
  };

  const replaceAssistant = (html) => {
    if (!live.assistant) return;
    const article = parseElement(
      html,
      `[data-assistant-message="${CSS.escape(live.assistant.article.dataset.assistantMessage)}"]`,
    );
    live.assistant.article.replaceWith(article);
    live.assistant = bindAssistant(article);
  };

  const startMessages = (data) => {
    const user = parseElement(data.user_html, "article");
    const assistantArticle = parseElement(
      data.assistant_html,
      `[data-assistant-message="${CSS.escape(String(data.message_id))}"]`,
    );
    messageList.querySelector("[data-coach-empty]")?.remove();
    messageList.append(user, assistantArticle);
    live.assistant = bindAssistant(assistantArticle);
    live.failureHtml = data.failure_html;
    if (typeof data.conversation_title === "string") {
      if (selectedTitle) selectedTitle.textContent = data.conversation_title;
      if (listTitle) listTitle.textContent = data.conversation_title;
    }
  };

  const handleEvent = async (name, data) => {
    if (name === "answer.started") {
      startMessages(data);
    } else if (name === "answer.delta" && live.assistant && typeof data.text === "string") {
      live.assistant.answer.append(document.createTextNode(data.text));
    } else if (name === "artifact.available" && live.assistant) {
      await addWorkoutArtifact(live.assistant.artifacts, conversationId, data);
    } else if (name === "answer.completed" || name === "answer.failed") {
      replaceAssistant(data.html);
    }
    scrollToBottom();
  };

  const consumeEvents = async (response) => {
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
    await consumeSse(response.body, handleEvent);
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = textarea.value.trim();
    if (!text || submit.disabled) return;

    live.assistant = null;
    live.failureHtml = null;
    textarea.value = "";
    textarea.style.height = "auto";
    submit.disabled = true;
    if (deleteButton) deleteButton.disabled = true;

    const body = new FormData(form);
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
      await consumeEvents(response);
    } catch (_) {
      if (live.assistant && live.failureHtml) replaceAssistant(live.failureHtml);
      else textarea.value = text;
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
