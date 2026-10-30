/* Resize the personal inspector without changing the workflow camera or app data. */
(() => {
  "use strict";
  const shell = document.querySelector(".app-shell");
  const inspector = document.getElementById("inspector");
  const handle = document.getElementById("inspector-resizer");
  if (!shell || !inspector || !handle) return;
  const key = "taskloom.inspectorWidth";
  let preferred = null, dragging = null;
  try {
    const saved = localStorage.getItem(key);
    if (saved !== null && Number.isFinite(Number(saved)) && Number(saved) > 0) preferred = Number(saved);
  } catch { /* Width remains usable without browser storage. */ }
  function bounds() {
    const max = window.innerWidth <= 1020 ? Math.max(1,window.innerWidth-24)
      : Math.max(260,Math.min(850,window.innerWidth-document.querySelector(".sidebar").getBoundingClientRect().width-380));
    return {min:Math.min(260,max),max};
  }
  function apply() {
    const {min,max} = bounds();
    if (preferred === null) shell.style.removeProperty("--inspector-width");
    else shell.style.setProperty("--inspector-width",`${Math.round(Math.max(min,Math.min(max,preferred)))}px`);
    const measured = inspector.getBoundingClientRect().width;
    handle.setAttribute("aria-valuemin",Math.min(min,Math.round(measured) || min));
    handle.setAttribute("aria-valuemax",max);
    handle.setAttribute("aria-valuenow",Math.round(measured) || Math.round(Math.max(min,Math.min(max,preferred || min))));
    handle.setAttribute("aria-valuetext",`${handle.getAttribute("aria-valuenow")} 像素`);
  }
  function remember() {
    try {
      if (preferred === null) localStorage.removeItem(key);
      else localStorage.setItem(key,String(preferred));
    } catch { /* Optional preference. */ }
  }
  function resize(width) {
    const {min,max}=bounds();preferred=Math.max(min,Math.min(max,width));apply();
  }
  handle.addEventListener("pointerdown",(event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    dragging={id:event.pointerId,x:event.clientX,width:inspector.getBoundingClientRect().width,original:preferred};
    handle.setPointerCapture(event.pointerId);handle.focus({preventScroll:true});
    document.body.classList.add("resizing-inspector");
  });
  handle.addEventListener("pointermove",(event) => {
    if (dragging?.id === event.pointerId) resize(dragging.width+dragging.x-event.clientX);
  });
  const finish=(event) => {
    if (dragging?.id !== event.pointerId) return;
    if (event.type === "pointercancel") { preferred=dragging.original;apply(); }
    dragging=null;document.body.classList.remove("resizing-inspector");remember();
  };
  handle.addEventListener("pointerup",finish);
  handle.addEventListener("pointercancel",finish);
  handle.addEventListener("lostpointercapture",finish);
  handle.addEventListener("dblclick",() => {preferred=null;apply();remember();});
  handle.addEventListener("keydown",(event) => {
    if (!["ArrowLeft","ArrowRight","Home","End"].includes(event.key)) return;
    event.preventDefault();
    if (event.key === "Home") {preferred=null;apply();}
    else if (event.key === "End") resize(bounds().max);
    else resize(inspector.getBoundingClientRect().width+(event.key === "ArrowLeft"?1:-1)*(event.shiftKey?50:20));
    remember();
  });
  window.addEventListener("resize",apply);
  new MutationObserver(apply).observe(shell,{attributes:true,attributeFilter:["class"]});
  apply();
})();
