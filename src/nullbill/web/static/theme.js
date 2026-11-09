// Before the page draws: the owner's light or dark choice, if they made one.
// Without one, style.css follows the system.
try {
  const theme = localStorage.getItem("nb-theme");
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
} catch (error) {
  // Storage off: follow the system.
}
