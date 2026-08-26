(() => {
  const token = document.querySelector('meta[name="csrf-token"]')?.content;
  if (!token) return;

  document.addEventListener("htmx:configRequest", (event) => {
    const method = String(event.detail.verb || "GET").toUpperCase();
    if (!["GET", "HEAD", "OPTIONS", "TRACE"].includes(method)) {
      event.detail.headers["X-CSRF-Token"] = token;
    }
  });
})();
