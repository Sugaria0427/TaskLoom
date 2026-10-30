"use strict";

(() => {
  const form = document.getElementById("demo-form");
  const fields = ["enabled", "tools", "models", "strength", "exponent", "text_reference_ms", "fixed_tool_ms", "model_reference_ms", "model_workload", "cap_seconds"];
  let saved = null;
  function read() {
    return Object.fromEntries(fields.map((key) => {
      const field = form.elements.namedItem(key);
      return [key, field.type === "checkbox" ? field.checked : Number(field.value)];
    }));
  }
  function target(settings, baseline, ratio, workload) {
    return Math.min(settings.cap_seconds, settings.strength * (workload ? Math.max(.1, baseline / 1000 * Math.pow(Math.max(ratio, .001), settings.exponent)) : baseline / 1000));
  }
  function seconds(value) { return `${value.toFixed(value < 1 ? 3 : 2)} s`; }
  function preview() {
    const config = read();
    document.getElementById("demo-strength-value").textContent = `${config.strength} 倍`;
    const svg = document.getElementById("demo-curve");
    const width = 420, height = 160, left = 44, bottom = 126, top = 16, right = 400;
    const curves = [
      {color:"#28755d", name:"文本工具", base:config.text_reference_ms, workload:true},
      {color:"#4e74b8", name:"模型请求", base:config.model_reference_ms, workload:config.model_workload},
      {color:"#b77b32", name:"固定工具", base:config.fixed_tool_ms, workload:false},
    ];
    const maximum = Math.max(.1, ...curves.map((curve) => target(config,curve.base,4,curve.workload)));
    svg.replaceChildren();
    function el(tag, attributes, text) {
      const element = document.createElementNS("http://www.w3.org/2000/svg",tag);
      for (const [key,value] of Object.entries(attributes)) element.setAttribute(key,String(value));
      if (text) element.textContent = text;
      svg.append(element); return element;
    }
    el("path",{d:`M${left} ${top}V${bottom}H${right}`,stroke:"#b9c7c0",fill:"none"});
    for (const fraction of [0,.5,1]) {
      const y = bottom - fraction * (bottom-top);
      el("line",{x1:left,y1:y,x2:right,y2:y,stroke:"#e5ece8"});
      el("text",{x:left-7,y:y+4,"text-anchor":"end"},`${(maximum*fraction).toFixed(1)}s`);
    }
    for (const curve of curves) {
      const points = Array.from({length:81},(_,index) => {
        const ratio = index/20;
        return `${left+ratio/4*(right-left)},${bottom-target(config,curve.base,ratio,curve.workload)/maximum*(bottom-top)}`;
      });
      el("polyline",{points:points.join(" "),stroke:curve.color,"stroke-width":2.5,fill:"none"});
    }
    for (const ratio of [0,1,2,4]) el("text",{x:left+ratio/4*(right-left),y:bottom+19,"text-anchor":"middle"},`${ratio}×`);
    document.getElementById("demo-samples").textContent = `22 KiB 文本：${seconds(target(config,config.text_reference_ms,1,true))}；固定工具：${seconds(target(config,config.fixed_tool_ms,1,false))}；1,000 输入估算 Token：${seconds(target(config,config.model_reference_ms,1,config.model_workload))}。`;
    for (const key of fields.filter((key) => key !== "enabled")) form.elements.namedItem(key).disabled = !config.enabled;
  }
  function fill(config) {
    saved = config;
    for (const key of fields) {
      const field = form.elements.namedItem(key);
      if (field.type === "checkbox") field.checked = config[key]; else field.value = config[key];
    }
    const badge = document.getElementById("demo-mode-badge");
    badge.hidden = !config.enabled; badge.textContent = `调试 · ${config.strength}×`;
    preview();
  }
  async function load() {
    try { fill(await api("/api/v1/settings/demo")); }
    catch (error) { document.getElementById("demo-status").textContent = error.message; }
  }
  form.addEventListener("input",preview);
  form.addEventListener("submit",async(event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    // Disabled controls keep their values, so turning the mode off preserves the curve.
    const config = read();
    document.getElementById("demo-save").disabled = true;
    try {
      fill(await api("/api/v1/settings/demo",{method:"POST",body:JSON.stringify(config)}));
      document.getElementById("demo-status").textContent = config.enabled ? "调试模式已开启，设置应用于新提交的任务。" : "调试模式已关闭，新任务使用实际耗时。";
    } catch (error) { document.getElementById("demo-status").textContent = error.message; }
    finally { document.getElementById("demo-save").disabled = false; }
  });
  document.getElementById("open-settings").addEventListener("click",load);
  window.addEventListener("taskloom:refresh",() => { if (!saved) load(); });
  load();
})();
