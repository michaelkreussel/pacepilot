function localizeDatetimes(root = document) {
  root.querySelectorAll("time[data-local-datetime]").forEach((element) => {
    const value = new Date(element.dateTime);
    if (Number.isNaN(value.getTime())) return;

    element.textContent = new Intl.DateTimeFormat("de-DE", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(value);
  });
}

document.addEventListener("DOMContentLoaded", () => localizeDatetimes());
document.addEventListener("htmx:afterSwap", (event) => localizeDatetimes(event.detail.target));
