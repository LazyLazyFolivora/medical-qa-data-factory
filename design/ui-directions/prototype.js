const params = new URLSearchParams(window.location.search);
const requested = params.get("theme");
const themes = {
  paper: { name: "A · LAB WHITE", bodyClass: "theme-paper" },
  console: { name: "B · GENOME CONSOLE", bodyClass: "theme-console" },
  editorial: { name: "C · ACADEMIC PRESS", bodyClass: "theme-editorial" },
};

function applyTheme(key) {
  const theme = themes[key] || themes.paper;
  document.body.classList.remove(...Object.values(themes).map((item) => item.bodyClass));
  document.body.classList.add(theme.bodyClass);
  document.getElementById("direction-name").textContent = theme.name;
  document.querySelectorAll("[data-theme]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.theme === key);
  });
  const next = new URL(window.location.href);
  next.searchParams.set("theme", key);
  window.history.replaceState({}, "", next);
}

document.querySelectorAll("[data-theme]").forEach((button) => {
  button.addEventListener("click", () => applyTheme(button.dataset.theme));
});

applyTheme(requested || "paper");
