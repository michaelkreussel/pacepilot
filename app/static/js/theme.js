(() => {
  const storageKey = "pacepilot-theme";
  const root = document.documentElement;
  const media = window.matchMedia("(prefers-color-scheme: dark)");

  const storedTheme = localStorage.getItem(storageKey);
  const initialTheme = storedTheme || (media.matches ? "dark" : "light");
  root.classList.toggle("dark", initialTheme === "dark");

  const updateControls = () => {
    const dark = root.classList.contains("dark");
    root.dataset.theme = dark ? "dark" : "light";
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.setAttribute("aria-pressed", String(dark));
      button.setAttribute("title", dark ? "Helles Design verwenden" : "Dunkles Design verwenden");
      button.setAttribute("aria-label", dark ? "Helles Design verwenden" : "Dunkles Design verwenden");
      const label = button.querySelector("[data-theme-label]");
      if (label) label.textContent = dark ? "Helles Design" : "Dunkles Design";
    });
    document.dispatchEvent(new CustomEvent("pacepilot:themechange"));
  };

  document.addEventListener("DOMContentLoaded", () => {
    updateControls();
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.addEventListener("click", () => {
        root.classList.toggle("dark");
        localStorage.setItem(storageKey, root.classList.contains("dark") ? "dark" : "light");
        updateControls();
      });
    });
  });

  media.addEventListener("change", (event) => {
    if (localStorage.getItem(storageKey)) return;
    root.classList.toggle("dark", event.matches);
    updateControls();
  });
})();
