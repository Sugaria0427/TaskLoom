"use strict";
/* 首帧前应用主题，避免深色模式闪白。偏好：system | light | dark，存 localStorage。 */
(function applyInitialTheme() {
  var mode = "system";
  try { mode = localStorage.getItem("taskloom.admin.theme") || "system"; } catch (e) { mode = "system"; }
  var dark = mode === "dark" || (mode !== "light" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", Boolean(dark));
}());
