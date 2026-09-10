export function parseElement(html, selector) {
  if (typeof html !== "string") throw new Error("Ungültige Nachrichtendarstellung.");
  const parsed = new DOMParser().parseFromString(html, "text/html").body.firstElementChild;
  if (!parsed?.matches(selector)) throw new Error("Ungültige Nachrichtendarstellung.");
  return document.importNode(parsed, true);
}

export async function addWorkoutArtifact(artifacts, conversationId, data) {
  if (
    artifacts.querySelector(`[data-workout-id="${CSS.escape(String(data.workout_id))}"]`)
  ) {
    return;
  }
  if (
    typeof data.card_url !== "string" ||
    !data.card_url.startsWith(`/coach/${conversationId}/messages/`)
  ) {
    return;
  }

  try {
    const response = await fetch(data.card_url, { headers: { Accept: "text/html" } });
    if (!response.ok || !response.headers.get("content-type")?.includes("text/html")) {
      throw new Error();
    }
    const card = parseElement(await response.text(), "[data-proposal-card]");
    if (card.dataset.workoutId !== String(data.workout_id)) throw new Error();
    artifacts.append(card);
  } catch (_) {
    const notice = document.createElement("p");
    notice.className = "mt-2 text-xs text-warning-emphasis";
    notice.setAttribute("role", "status");
    notice.textContent = "Der Vorschlag wurde gespeichert. Lade den Chat neu, um die Karte zu öffnen.";
    artifacts.append(notice);
  }
}
