// Runs before the first paint (a plain file, because the page's security policy allows no inline scripts).
// Sets data-theme to "light" or "dark" when the person chose one; otherwise the CSS follows the system setting.
(function () {
  try {
    var saved = localStorage.getItem("rectifi.theme");
    if (saved === "light" || saved === "dark") document.documentElement.setAttribute("data-theme", saved);
  } catch (e) {}
})();
