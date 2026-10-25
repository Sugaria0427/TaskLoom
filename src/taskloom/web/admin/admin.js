"use strict";

/* 平台调度控制台：只读快照轮询 + 调度配置。数据一律经 textContent 写入。 */

const ENDPOINT = "/admin/v1/scheduler";
const SETTINGS_ENDPOINT = "/admin/v1/scheduler/settings";
const KEY_STORAGE = "taskloom.admin.key";
const THEME_STORAGE = "taskloom.admin.theme";
const POLL_MS = 2000;
const REQUEST_TIMEOUT_MS = 10000;

const POLICIES = {
  rr: {name: "rr", title: "轮转（默认）", note: "有排队的账号依次轮流派发，每轮一个请求。"},
  hybrid: {name: "hybrid", title: "候选", note: "优先派发当前占槽最少的账号，同数时轮转。"},
  drr: {name: "drr", title: "累计占槽秒", note: "按累计占用槽位的时长，补偿占得少的账号。"},
  maxmin: {name: "maxmin", title: "当前占槽 max-min", note: "让各账号当前占槽数尽量均衡（最小者优先）。"},
};

/* blocked_by → [中文, 号池色调]：danger 预算类，warning 窗口/退避，accent 容量类，muted 只是没轮到。 */
const BLOCKED = {
  backoff: ["退避中", "warning"],
  pool: ["池已满", "accent"],
  window: ["平台窗口", "warning"],
  budget: ["平台预算", "danger"],
  account_inflight: ["账号并发上限", "accent"],
  account_window: ["账号窗口", "warning"],
  account_budget: ["账号预算", "danger"],
  turn: ["等待轮到", "muted"],
};

const EVENT_KINDS = {
  dispatch: ["派发", "dispatch"],
  finish: ["完成结算", "finish"],
  reject: ["拒绝", "reject"],
  unknown: ["结果未知", "unknown"],
  expire: ["过期", "expire"],
  cancel: ["取消", "cancel"],
  settings: ["配置变更", "settings"],
};

const ERROR_MESSAGES = {
  ADMIN_DISABLED: "平台未启用管理接口：需在平台配置中设置 admin_key 后重启平台服务。",
  ADMIN_UNAUTHORIZED: "admin key 不正确，请重新输入。",
  INVALID_INPUT: "配置参数无效。",
  PLATFORM_UNAVAILABLE: "平台暂不可用，正在重试。",
};

const THEMES = {
  system: {label: "跟随系统", icon: "monitor", next: "light"},
  light: {label: "浅色", icon: "sun", next: "dark"},
  dark: {label: "深色", icon: "moon", next: "system"},
};

const state = {
  key: "", snapshot: null, timer: null, controller: null, polling: false,
  authError: null, netError: "", expanded: new Set(), formDirty: false,
  applying: false, lastOk: null, generation: 0, autoRefresh: true, theme: "system",
};

/* ---------- 纯函数 ---------- */

function num(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "—";
  return num(value).toLocaleString("zh-CN");
}

function formatCompact(value) {
  const n = num(value);
  const abs = Math.abs(n);
  if (abs < 10000) return n.toLocaleString("zh-CN");
  if (abs < 1e6) return `${(n / 1000).toFixed(abs < 1e5 ? 1 : 0).replace(/\.0$/, "")}k`;
  return `${(n / 1e6).toFixed(abs < 1e7 ? 2 : 1).replace(/\.?0+$/, "")}M`;
}

function formatSeconds(value) {
  if (value === null || value === undefined) return "—";
  const s = Math.max(0, num(value));
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)} 秒`;
  if (s < 3600 && s % 60 === 0) return `${s / 60} 分钟`;
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒`;
  return `${Math.floor(s / 3600)} 时 ${Math.floor((s % 3600) / 60)} 分`;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleTimeString("zh-CN", {hour12: false});
}

function ratio(used, total) {
  const t = num(total);
  if (t <= 0) return 0;
  return Math.min(1, Math.max(0, num(used) / t));
}

function percent(used, total) { return `${(ratio(used, total) * 100).toFixed(2)}%`; }

/* 号池配额条阈值：≥90% 红，≥70% 黄，否则绿。 */
function quotaTone(used, total) {
  const r = ratio(used, total);
  return r >= 0.9 ? "q-bad" : r >= 0.7 ? "q-warn" : "q-good";
}

function policyLabel(name) {
  const p = POLICIES[name];
  return p ? `${name} · ${p.title}` : String(name || "—");
}

function blockedLabel(code) {
  if (!code) return null;
  return BLOCKED[code] || [String(code), "muted"];
}

function eventLabel(kind) { return EVENT_KINDS[kind] || [String(kind || "—"), "other"]; }

function budgetParts(entry) {
  const budget = Math.max(0, num(entry.budget_units));
  const consumed = Math.max(0, num(entry.consumed_units));
  const worst = Math.max(0, num(entry.worst_units));
  const remaining = Math.max(0, budget - consumed - worst);
  return {budget, consumed, worst, reserved: Math.max(0, num(entry.reserved_units)), remaining};
}

/* 占槽份额：每个账号 running / capacity；有需求账号（在跑或排队）平分 capacity 作为参考线。 */
function shareModel(snapshot) {
  const capacity = Math.max(0, num(snapshot?.pool?.capacity));
  const tenants = Array.isArray(snapshot?.tenants) ? snapshot.tenants : [];
  const demanding = tenants.filter((t) => num(t.running) > 0 || num(t.queued) > 0);
  const anyQueued = tenants.some((t) => num(t.queued) > 0);
  const fair = demanding.length ? capacity / demanding.length : 0;
  return {
    capacity, fair, demanding: demanding.length, showFair: anyQueued && capacity > 0,
    rows: demanding.map((t) => ({
      account_id: String(t.account_id), running: num(t.running), queued: num(t.queued),
      share: ratio(t.running, capacity),
    })),
  };
}

function errorCode(data) {
  if (!data || typeof data !== "object") return "";
  return data.error?.code || data.code || data.detail?.code || "";
}

function errorText(data) {
  if (!data || typeof data !== "object") return "";
  const message = data.error?.message || data.message || data.detail?.message;
  return typeof message === "string" ? message : typeof data.detail === "string" ? data.detail : "";
}

/* ---------- DOM 工具 ---------- */

const $ = (id) => document.getElementById(id);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function node(tag, className = "", text = "") {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== "") el.textContent = String(text);
  return el;
}

function icon(name, className = "icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", className);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function setIcon(button, name) {
  const use = button.querySelector("use");
  if (use) use.setAttribute("href", `#i-${name}`);
}

function setText(id, value) { $(id).textContent = String(value); }

function idText(text, className = "id") {
  return node("span", className, text === null || text === undefined || text === "" ? "—" : text);
}

function tone(label, kind) { return node("span", `tone ${kind}`, label); }

function bar(used, total, fillClass = "", thin = false) {
  const track = node("div", thin ? "bar thin" : "bar");
  const fill = node("span", `bar-fill ${fillClass}`.trim());
  fill.style.width = percent(used, total);
  track.append(fill);
  return track;
}

function stackBar(entry, thin = false) {
  const p = budgetParts(entry);
  const track = node("div", thin ? "stack thin" : "stack");
  track.setAttribute("role", "img");
  track.setAttribute("aria-label",
    `预算 ${formatNumber(p.budget)}：已结算 ${formatNumber(p.consumed)}，在途最坏 ${formatNumber(p.worst)}，剩余 ${formatNumber(p.remaining)}`);
  const consumed = node("span", "seg consumed");
  consumed.style.width = percent(p.consumed, p.budget);
  const worst = node("span", "seg worst");
  worst.style.width = percent(Math.min(p.worst, Math.max(0, p.budget - p.consumed)), p.budget);
  track.append(consumed, worst);
  return track;
}

function meterRow(label, value, labelClass = "") {
  const row = node("div", "meter-row");
  const name = node("span", labelClass, label);
  name.title = label;
  row.append(name, node("strong", "", value));
  return row;
}

function cellBar(label, used, total, fillClass) {
  const box = node("div", "cell-bar");
  box.title = `${label} ${formatNumber(used)} / ${formatNumber(total)}`;
  const top = node("div", "bar-label");
  const value = node("b", "", `${formatCompact(used)} / ${formatCompact(total)}`);
  top.append(node("span", "", label), value);
  box.append(top, bar(used, total, fillClass, true));
  return box;
}

function emptyRow(colspan, text) {
  const tr = node("tr", "empty-row");
  const td = node("td", "", text);
  td.colSpan = colspan;
  tr.append(td);
  return tr;
}

function emptyDashed(text, iconName = "inbox", extra = "") {
  const box = node("div", `empty-dashed ${extra}`.trim());
  box.append(icon(iconName), node("span", "", text));
  return box;
}

function emptyState(title, desc, iconName = "inbox") {
  const box = node("div", "empty-state");
  const circle = node("span", "circle");
  circle.append(icon(iconName));
  box.append(circle, node("h4", "", title), node("p", "", desc));
  return box;
}

/* ---------- Toast（ToastWithProgress：顶部居中 + 进度环） ---------- */

const TOAST_ICONS = {success: "check-circle", error: "x-circle", warning: "alert", info: "info"};
const RING = 2 * Math.PI * 14;

function toast(message, type = "success", title = "") {
  const box = $("toasts");
  while (box.children.length >= 3) box.firstElementChild.remove();
  const duration = type === "error" || type === "warning" ? 8000 : 5000;
  const item = node("div", `toast ${type}`);
  item.setAttribute("role", type === "error" ? "alert" : "status");
  const ring = node("div", "toast-ring");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "ring");
  svg.setAttribute("viewBox", "0 0 32 32");
  svg.setAttribute("aria-hidden", "true");
  const circle = (cls) => {
    const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    c.setAttribute("class", cls);
    c.setAttribute("cx", "16");
    c.setAttribute("cy", "16");
    c.setAttribute("r", "14");
    return c;
  };
  const progress = circle("progress");
  progress.style.strokeDashoffset = "0";
  svg.append(circle("track"), progress);
  ring.append(svg, icon(TOAST_ICONS[type] || "info"));
  const text = node("div", "toast-text");
  if (title) text.append(node("p", "toast-title", title));
  text.append(node("p", title ? "toast-msg" : "toast-title", message));
  const close = node("button", "toast-close");
  close.type = "button";
  close.setAttribute("aria-label", "关闭通知");
  close.append(icon("x"));
  item.append(ring, text, close);
  box.append(item);

  const started = performance.now();
  let frame = 0;
  let removed = false;
  const remove = () => {
    if (removed) return;
    removed = true;
    cancelAnimationFrame(frame);
    item.classList.add("leaving");
    setTimeout(() => item.remove(), 200);
  };
  const tick = (now) => {
    const left = Math.max(0, 1 - (now - started) / duration);
    progress.style.strokeDashoffset = String(RING * (1 - left));
    if (left > 0 && !removed) frame = requestAnimationFrame(tick);
  };
  frame = requestAnimationFrame(tick);
  setTimeout(remove, duration + 100);
  close.addEventListener("click", remove);
}

/* ---------- 确认对话框（Aether Dialog） ---------- */

let dialogResolve = null;
let dialogReturnFocus = null;

function confirmDialog({title, description, lines = [], confirmText = "确认"}) {
  const root = $("dialog");
  setText("dialog-title", title);
  setText("dialog-desc", description || "");
  const body = $("dialog-body");
  body.replaceChildren();
  for (const line of lines) body.append(node("p", line.highlight ? "dialog-highlight" : "", line.text));
  setText("dialog-confirm", confirmText);
  dialogReturnFocus = document.activeElement;
  root.hidden = false;
  $("dialog-confirm").focus();
  return new Promise((resolve) => { dialogResolve = resolve; });
}

function closeDialog(result) {
  const root = $("dialog");
  if (root.hidden) return;
  root.hidden = true;
  const resolve = dialogResolve;
  dialogResolve = null;
  if (dialogReturnFocus && typeof dialogReturnFocus.focus === "function") dialogReturnFocus.focus();
  if (resolve) resolve(result);
}

function onDialogKey(event) {
  if ($("dialog").hidden) return;
  if (event.key === "Escape") { event.preventDefault(); closeDialog(false); return; }
  if (event.key === "Tab") {
    const focusable = [$("dialog-confirm"), $("dialog-cancel")];
    const order = [...$("dialog").querySelectorAll("button")].filter((b) => focusable.includes(b));
    const index = order.indexOf(document.activeElement);
    event.preventDefault();
    const next = event.shiftKey ? (index <= 0 ? order.length - 1 : index - 1) : (index + 1) % order.length;
    order[next].focus();
  }
}

/* ---------- 渲染 ---------- */

function readiness(s) {
  if (!state.key || state.authError) return ["", "未连接"];
  if (state.netError) return ["bad", "连接中断"];
  if (!s) return ["", "连接中…"];
  return s.ready ? ["ok", "调度就绪"] : ["warn", "未就绪"];
}

function renderChrome(s) {
  const locked = !state.key || Boolean(state.authError);
  document.body.classList.toggle("is-locked", locked);
  $("login-view").hidden = !locked;
  $("console-view").hidden = locked;

  const [level, label] = readiness(s);
  $("ready-pill").className = `status-pill ${level}`.trim();
  setText("ready-text", label);
  for (const dot of $$("[data-ready-dot]")) dot.className = `live-dot ${level}`.trim();
  const time = state.lastOk ? formatTime(state.lastOk) : "—";
  setText("refreshed-at", time);
  setText("updated-at", time);

  for (const el of $$("[data-key-status]")) {
    el.textContent = !state.key ? "未登录" : state.authError ? "key 无效或已停用" : state.netError ? "连接中断" : "已连接 · 本标签页";
    el.className = `side-user-role${!state.key ? "" : state.authError || state.netError ? " bad" : " ok"}`;
  }
  for (const button of $$("[data-logout]")) button.hidden = !state.key;
  for (const sw of $$("[data-auto-toggle]")) {
    sw.setAttribute("aria-checked", String(state.autoRefresh));
    sw.title = state.autoRefresh ? `每 ${POLL_MS / 1000} 秒自动刷新，点击暂停` : "已暂停自动刷新，点击恢复";
  }
  $("refresh-now").disabled = locked;
  $("refresh-now").classList.toggle("spinning", state.polling);

  /* 登录卡 */
  $("login-error").hidden = !state.authError;
  setText("login-error-text", state.authError || "");
  $("key-clear").hidden = !state.key;
  setText("key-save", state.authError ? "使用新 key 连接" : "连接");

  /* 网络告警 */
  $("net-alert").hidden = !state.netError || locked;
  setText("net-alert-text", state.netError || "");

  /* 导航上的状态 */
  const unknownCount = num(s?.pool?.unknown);
  for (const el of $$("[data-unknown-count]")) {
    el.hidden = !(s && unknownCount > 0);
    el.textContent = formatNumber(unknownCount);
  }
  const upstreamOff = Boolean(s) && !s.placement;
  for (const el of $$("[data-upstream-tag]")) el.hidden = !upstreamOff;
  for (const link of $$('[data-nav="upstream"]')) {
    link.classList.toggle("is-off", upstreamOff);
    link.title = upstreamOff ? "未配置多上游账号（placement=false）" : "";
  }
}

function renderPageHead(s) {
  setText("policy-badge", s ? `策略 ${s.policy || "—"}` : "策略 —");
  const pool = s?.pool;
  setText("page-desc", pool
    ? `L4 模型请求调度实时状态 · 模型 ${pool.model || "—"} · ${pool.pool_kind || "—"} 池${s.placement ? " · placement 多上游" : ""}`
    : `L4 模型请求调度的实时状态，每 ${POLL_MS / 1000} 秒刷新。`);
}

function kpiValue(id, main, unit = "", cls = "") {
  const el = $(id);
  el.className = `kpi-value ${cls}`.trim();
  el.replaceChildren(document.createTextNode(main));
  if (unit) el.append(node("span", "unit", unit));
}

function renderKpis(s) {
  const pool = s?.pool || {};
  const slots = $("kpi-slots");
  slots.replaceChildren();
  if (s) {
    slots.append(document.createTextNode(formatNumber(pool.inflight)), node("span", "sep", "/"), document.createTextNode(formatNumber(pool.capacity)));
  } else {
    slots.textContent = "— / —";
  }
  const fill = $("kpi-slots-bar");
  fill.style.width = s ? percent(pool.inflight, pool.capacity) : "0%";
  fill.className = `bar-fill${s && ratio(pool.inflight, pool.capacity) >= 1 && num(pool.queued) > 0 ? " q-warn" : ""}`;
  setText("kpi-slots-hint", s ? `在途 / 容量 · 利用率 ${Math.round(ratio(pool.inflight, pool.capacity) * 100)}%` : "在途 / 容量");

  const tenants = Array.isArray(s?.tenants) ? s.tenants : [];
  const queuedAccounts = tenants.filter((t) => num(t.queued) > 0).length;
  kpiValue("kpi-queued", s ? formatNumber(pool.queued) : "—");
  setText("kpi-queued-hint", s ? (num(pool.queued) ? `${queuedAccounts} 个账号有排队请求` : "没有等待派发的请求") : "等待派发的请求");

  kpiValue("kpi-unknown", s ? formatNumber(pool.unknown) : "—", "", num(pool.unknown) > 0 ? "warn" : "");

  kpiValue("kpi-policy", s ? String(s.policy || "—") : "—");
  setText("kpi-policy-note", !s ? "—" : s.policy_changed_at
    ? `${POLICIES[s.policy]?.title || "后端策略"} · ${formatTime(s.policy_changed_at)} 切换`
    : `${POLICIES[s.policy]?.title || "后端策略"} · 来自启动配置`);

  const sh = s?.shared;
  if (sh) {
    const p = budgetParts(sh);
    const left = p.budget > 0 ? (p.remaining / p.budget) * 100 : 0;
    kpiValue("kpi-budget", p.budget > 0 ? left.toFixed(1) : "—", p.budget > 0 ? "%" : "", left < 10 ? "bad" : left < 30 ? "warn" : "");
    setText("kpi-budget-hint", `剩余 ${formatNumber(p.remaining)} / ${formatNumber(p.budget)} units`);
    const remain = Math.max(0, num(sh.window_remaining_seconds));
    kpiValue("kpi-window", remain < 10 ? remain.toFixed(1) : String(Math.round(remain)), "秒");
    setText("kpi-window-hint", `窗口 ${formatSeconds(sh.window_seconds)} · 请求 ${formatNumber(sh.window_requests)} / ${formatNumber(sh.requests_per_window)}`);
  } else {
    kpiValue("kpi-budget", "—");
    setText("kpi-budget-hint", "—");
    kpiValue("kpi-window", "—");
    setText("kpi-window-hint", "—");
  }
}

function renderShareChart(s) {
  const box = $("share-body");
  box.replaceChildren();
  const model = shareModel(s);
  setText("share-badge", s ? `${model.capacity} 槽 · ${model.demanding} 个有需求账号` : "—");
  if (!s || model.capacity <= 0 || !model.rows.length) {
    box.append(emptyDashed(!s ? "连接后显示占槽份额。" : model.capacity <= 0 ? "池容量为 0。" : "当前没有在跑或排队的账号。"));
    return;
  }
  const list = node("div", "share-rows");
  for (const row of model.rows) {
    const line = node("div", "share-row");
    const name = node("span", "share-name", row.account_id);
    name.title = row.account_id;
    const track = node("div", "share-track");
    track.setAttribute("role", "img");
    track.setAttribute("aria-label", `${row.account_id} 占 ${row.running} / ${model.capacity} 槽，排队 ${row.queued}`);
    const fill = node("span", "share-fill");
    fill.style.width = `${(row.share * 100).toFixed(2)}%`;
    track.append(fill);
    if (model.capacity > 1 && model.capacity <= 32) {
      const ticks = node("span", "share-ticks");
      ticks.style.setProperty("--slots", String(model.capacity));
      track.append(ticks);
    }
    if (model.showFair) {
      const mark = node("span", "share-fair");
      mark.style.left = percent(model.fair, model.capacity);
      track.append(mark);
    }
    const value = node("span", "share-value");
    value.append(node("strong", "", `${row.running}/${model.capacity}`));
    if (row.queued) value.append(document.createTextNode(` · 排 ${row.queued}`));
    line.append(name, track, value);
    list.append(line);
  }
  const foot = node("div", "share-foot");
  const legend = node("div", "legend");
  const item = (swatchClass, label) => {
    const el = node("span", "legend-item");
    el.append(node("i", `swatch ${swatchClass}`), node("span", "", label));
    return el;
  };
  legend.append(item("consumed", "在跑占槽"));
  if (model.showFair) legend.append(item("fair", "平均应得"));
  foot.append(legend);
  if (model.showFair) {
    const fairText = Number.isInteger(model.fair) ? String(model.fair) : model.fair.toFixed(2);
    foot.append(node("span", "", `平均应得 = ${model.capacity} 槽 ÷ ${model.demanding} 个有需求账号 = ${fairText} 槽`));
  } else {
    foot.append(node("span", "", "没有排队时不显示平均应得参考线"));
  }
  box.append(list, foot);
}

function rootsRow(tenant) {
  const tr = node("tr", "roots-row");
  const td = node("td");
  td.colSpan = 7;
  const roots = Array.isArray(tenant.roots) ? tenant.roots : [];
  if (!roots.length) {
    td.append(emptyDashed("没有在跑或排队的根任务。"));
  } else {
    td.append(node("p", "roots-title", `根任务明细 · ${roots.length} 个`));
    const wrap = node("div", "mini-table-wrap");
    const table = node("table", "mini-table");
    const head = node("tr");
    for (const [label, cls] of [["根任务", ""], ["在跑", "num"], ["排队", "num"], ["最长等待", "num"]]) {
      const th = node("th", cls, label);
      th.scope = "col";
      head.append(th);
    }
    const thead = node("thead");
    thead.append(head);
    const body = node("tbody");
    for (const root of roots) {
      const row = node("tr");
      const idCell = node("td");
      idCell.append(idText(root.root_task_id, "id-strong"));
      row.append(idCell, node("td", "num", formatNumber(root.running)), node("td", "num", formatNumber(root.queued)),
        node("td", "num", num(root.queued) ? formatSeconds(root.oldest_wait_seconds) : "—"));
      body.append(row);
    }
    table.append(thead, body);
    wrap.append(table);
    td.append(wrap);
  }
  tr.append(td);
  return tr;
}

function renderTenants(s) {
  const body = $("tenants-body");
  body.replaceChildren();
  const tenants = Array.isArray(s?.tenants) ? s.tenants : [];
  const active = tenants.filter((t) => num(t.running) || num(t.queued) || num(t.unknown)).length;
  setText("tenants-note", s ? `| ${tenants.length} 个账号 · ${active} 个有请求` : "");
  if (!tenants.length) {
    body.append(emptyRow(7, s ? "当前没有账号记录。" : "连接后显示账号。"));
    return;
  }
  const seen = new Set();
  for (const t of tenants) {
    const id = String(t.account_id);
    seen.add(id);
    const open = state.expanded.has(id);
    const tr = node("tr", open ? "tenant-row open" : "tenant-row");
    const first = node("td");
    const cell = node("div", "tenant-cell");
    const toggle = node("button", "row-toggle");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", `${open ? "收起" : "展开"} ${id} 的根任务明细`);
    toggle.dataset.account = id;
    toggle.append(icon("chevron"));
    const label = node("span", "tenant-label");
    const name = node("span", "tenant-name", id);
    name.title = id;
    label.append(name);
    if (num(t.unknown) > 0) {
      const chip = tone(`待核对 ${formatNumber(t.unknown)}`, "warning");
      chip.classList.add("unknown-chip");
      label.append(chip);
    }
    cell.append(toggle, label);
    first.append(cell);

    const slots = node("td", "num");
    const strong = node("span", "slots", formatNumber(t.running));
    strong.append(node("span", "of", ` / ${formatNumber(t.max_inflight)}`));
    slots.append(strong);
    const queued = node("td", num(t.queued) ? "num" : "num zero", formatNumber(t.queued));
    const wait = node("td", num(t.queued) ? "num" : "num zero", num(t.queued) ? formatSeconds(t.oldest_wait_seconds) : "—");

    const budget = node("td");
    const bp = budgetParts(t);
    const bwrap = node("div", "cell-bar");
    bwrap.title = `预算 ${formatNumber(bp.budget)}：已结算 ${formatNumber(bp.consumed)}，在途最坏 ${formatNumber(bp.worst)}`;
    const btop = node("div", "bar-label");
    btop.append(node("span", "", `${Math.round(ratio(bp.consumed + bp.worst, bp.budget) * 100)}%`),
      node("b", "", `${formatCompact(bp.consumed + bp.worst)} / ${formatCompact(bp.budget)}`));
    bwrap.append(btop, stackBar(t, true));
    budget.append(bwrap);

    const windows = node("td");
    const bars = node("div", "cell-bars");
    bars.append(
      cellBar("请求", t.window_requests, t.requests_per_window, quotaTone(t.window_requests, t.requests_per_window)),
      cellBar("Token", t.window_tokens, t.tokens_per_window, quotaTone(t.window_tokens, t.tokens_per_window)),
    );
    windows.append(bars);

    const blocked = node("td");
    const reason = blockedLabel(t.blocked_by);
    blocked.append(reason ? tone(reason[0], reason[1]) : node("span", "zero", "—"));

    tr.append(first, slots, queued, wait, budget, windows, blocked);
    body.append(tr);
    if (open) body.append(rootsRow(t));
  }
  for (const id of [...state.expanded]) if (!seen.has(id)) state.expanded.delete(id);
}

function renderShared(s) {
  const box = $("shared-ledger");
  box.replaceChildren();
  const sh = s?.shared;
  if (!sh) {
    box.append(emptyDashed(s ? "暂无账本数据。" : "连接后显示共享账本。"));
    setText("shared-note", "—");
    return;
  }
  const p = budgetParts(sh);
  setText("shared-note", `窗口 ${formatSeconds(sh.window_seconds)}`);
  const grid = node("div", "metric-grid");
  for (const [label, value, swatch] of [
    ["已结算", p.consumed, "consumed"], ["在途最坏", p.worst, "worst"],
    ["当前预留", p.reserved, ""], ["剩余", p.remaining, "remaining"],
  ]) {
    const cell = node("div", "metric");
    const l = node("div", "l");
    if (swatch) l.append(node("i", `swatch ${swatch}`));
    l.append(node("span", "", label));
    const v = node("div", "v", formatNumber(value));
    v.title = `${formatNumber(value)} units`;
    cell.append(l, v);
    grid.append(cell);
  }
  const budget = node("div");
  budget.append(meterRow("平台预算", `${formatNumber(p.consumed + p.worst)} / ${formatNumber(p.budget)}`), stackBar(sh));
  budget.append(node("p", "meter-foot", "已结算 + 在途最坏费用；准入按最坏费用预留"));
  const req = node("div");
  req.append(meterRow("请求窗口", `${formatNumber(sh.window_requests)} / ${formatNumber(sh.requests_per_window)}`),
    bar(sh.window_requests, sh.requests_per_window, quotaTone(sh.window_requests, sh.requests_per_window)));
  const tok = node("div");
  tok.append(meterRow("Token 窗口", `${formatNumber(sh.window_tokens)} / ${formatNumber(sh.tokens_per_window)}`),
    bar(sh.window_tokens, sh.tokens_per_window, quotaTone(sh.window_tokens, sh.tokens_per_window)));
  tok.append(node("p", "meter-foot", `本窗口剩余 ${formatSeconds(sh.window_remaining_seconds)}`));
  box.append(grid, budget, req, tok);
}

function renderUpstream(s) {
  const section = $("upstream");
  const list = $("upstream-list");
  list.replaceChildren();
  const upstream = Array.isArray(s?.upstream) ? s.upstream : [];
  section.hidden = !(s && s.placement);
  if (section.hidden) return;
  const busy = upstream.reduce((a, u) => a + num(u.busy), 0);
  const cap = upstream.reduce((a, u) => a + num(u.capacity), 0);
  setText("upstream-note", `${upstream.length} 个 · ${busy} / ${cap}`);
  if (!upstream.length) {
    list.append(emptyDashed("未报告上游账号。", "server"));
    return;
  }
  const wrap = node("div", "quota-list");
  for (const u of upstream) {
    const item = node("div", "quota");
    item.append(meterRow(String(u.account_id), `${formatNumber(u.busy)} / ${formatNumber(u.capacity)}`, "id-strong"),
      bar(u.busy, u.capacity, quotaTone(u.busy, u.capacity), true));
    wrap.append(item);
  }
  list.append(wrap);
}

function renderUnknown(s) {
  const list = $("unknown-list");
  list.replaceChildren();
  const items = Array.isArray(s?.unknown) ? s.unknown : [];
  const note = $("unknown-note");
  note.textContent = s ? `${items.length} 条${items.length >= 50 ? "（最多 50）" : ""}` : "—";
  note.className = `badge ${items.length ? "warn-soft" : "outline"}`;
  if (!items.length) {
    list.append(s ? emptyDashed("没有待核对的请求。", "check-circle", "good") : emptyDashed("连接后显示待核对请求。"));
    return;
  }
  const wrap = node("div", "unknown-items");
  for (const item of items) {
    const card = node("article", "unknown-item");
    const top = node("div", "unknown-top");
    top.append(idText(item.request_id, "id-strong"), node("span", "id", formatTime(item.dispatched_at)));
    const meta = node("p", "unknown-meta");
    meta.append(document.createTextNode("账号 "), idText(item.account_id, "id"));
    if (item.upstream_account) meta.append(document.createTextNode(" · 上游 "), idText(item.upstream_account, "id"));
    card.append(top, meta);
    if (item.error) card.append(node("p", "unknown-error", item.error));
    wrap.append(card);
  }
  list.append(wrap);
}

function renderEvents(s) {
  const body = $("events-body");
  body.replaceChildren();
  const events = Array.isArray(s?.events) ? s.events : [];
  setText("events-note", s ? `| ${events.length} 条 · 最新在前` : "");
  const legend = $("events-legend");
  if (!legend.childElementCount) {
    for (const [, [label, kind]] of Object.entries(EVENT_KINDS)) {
      const item = node("span", "legend-item");
      item.append(node("i", `kind-dot k-${kind}`), node("span", "", label));
      legend.append(item);
    }
  }
  if (!events.length) {
    const tr = node("tr", "empty-row");
    const td = node("td");
    td.colSpan = 7;
    td.append(emptyState(s ? "还没有调度事件" : "等待连接", s ? "派发、结算、拒绝和配置变更会实时出现在这里。" : "连接平台后显示最近 200 条调度事件。", "activity"));
    tr.append(td);
    body.append(tr);
    return;
  }
  for (const e of events) {
    const tr = node("tr");
    const [label, kind] = eventLabel(e.kind);
    const kindCell = node("td");
    const k = node("span", "kind");
    k.append(node("i", `kind-dot k-${kind}`), node("span", "", label));
    kindCell.append(k);
    const cell = (value) => { const td = node("td"); td.append(idText(value, value ? "id-strong" : "id")); return td; };
    const req = node("td", "req");
    req.append(idText(e.request_id, e.request_id ? "id-strong" : "id"));
    if (e.upstream_account) req.append(node("span", "at", `@${e.upstream_account}`));
    const detail = node("td", "detail", e.detail || "—");
    if (e.detail) detail.title = e.detail;
    tr.append(
      node("td", "time", formatTime(e.at)), kindCell, cell(e.account_id), cell(e.root_task_id), req,
      node("td", e.units === null || e.units === undefined ? "num zero" : "num units", e.units === null || e.units === undefined ? "—" : formatNumber(e.units)),
      detail,
    );
    body.append(tr);
  }
}

function renderSettings(s) {
  const options = $("policy-options");
  const names = Array.isArray(s?.policies) && s.policies.length ? s.policies : Object.keys(POLICIES);
  const existing = [...options.querySelectorAll("input[name=policy]")].map((i) => i.value);
  if (existing.join(",") !== names.join(",")) {
    [...options.querySelectorAll(".policy-card")].forEach((el) => el.remove());
    for (const name of names) {
      const meta = POLICIES[name] || {title: name, note: "后端提供的策略。"};
      const label = node("label", "policy-card");
      label.dataset.policy = name;
      const input = node("input");
      input.type = "radio";
      input.name = "policy";
      input.value = name;
      const radio = node("span", "policy-radio");
      radio.setAttribute("aria-hidden", "true");
      const text = node("span", "policy-text");
      const title = node("span", "policy-title");
      const current = node("span", "current-chip", "当前");
      current.hidden = true;
      title.append(node("span", "policy-code", name), node("span", "", meta.title), current);
      text.append(title, node("span", "policy-note", meta.note));
      label.append(input, radio, text);
      options.append(label);
    }
  }
  const cache = $("cache-wait");
  const placement = Boolean(s?.placement);
  cache.disabled = !s || !placement || state.applying;
  setText("cache-wait-note", placement
    ? "缓存亲和最多等待的秒数（0–600）。"
    : "未配置多上游账号（placement=false），此项不生效，暂不可修改。");
  if (!state.formDirty) {
    for (const input of options.querySelectorAll("input[name=policy]")) input.checked = Boolean(s) && input.value === s.policy;
    cache.value = s ? String(num(s.cache_wait_seconds)) : "";
  }
  for (const card of options.querySelectorAll(".policy-card")) {
    const input = card.querySelector("input");
    input.disabled = !s || state.applying;
    card.classList.toggle("selected", input.checked);
    card.classList.toggle("disabled", input.disabled);
    card.querySelector(".current-chip").hidden = !(s && input.value === s.policy);
  }
  const note = $("settings-note");
  note.textContent = s ? (state.formDirty ? "有未应用的修改" : `当前 ${s.policy}`) : "—";
  note.className = `badge ${s && state.formDirty ? "warn-soft" : s ? "primary-soft" : "outline"}`;
  $("settings-apply").disabled = !s || state.applying || !state.formDirty;
  $("settings-apply").textContent = state.applying ? "应用中…" : "应用";
  $("settings-reset").hidden = !state.formDirty || state.applying;
}

function render() {
  const s = state.snapshot;
  renderChrome(s);
  renderPageHead(s);
  renderKpis(s);
  renderShareChart(s);
  renderTenants(s);
  renderShared(s);
  renderUpstream(s);
  renderUnknown(s);
  renderEvents(s);
  renderSettings(s);
  updateActiveNav();
}

/* ---------- 网络 ---------- */

async function adminFetch(path, options = {}) {
  const controller = new AbortController();
  state.controller = controller;
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(path, {
      ...options, signal: controller.signal, cache: "no-store", credentials: "omit",
      headers: {"Accept": "application/json", "Authorization": `Bearer ${state.key}`, ...(options.headers || {})},
    });
    let data = null;
    try { data = await response.json(); } catch { data = null; }
    if (!response.ok) {
      const error = new Error(errorText(data) || `HTTP ${response.status}`);
      error.status = response.status;
      error.code = errorCode(data) || `HTTP_${response.status}`;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError") {
      const timeout = new Error("请求超时");
      timeout.code = "TIMEOUT";
      throw timeout;
    }
    throw error;
  } finally {
    clearTimeout(timer);
    if (state.controller === controller) state.controller = null;
  }
}

function isAuthError(error) {
  return error.status === 401 || error.status === 403
    || error.code === "ADMIN_DISABLED" || error.code === "ADMIN_UNAUTHORIZED";
}

function authMessage(error) {
  if (error.code === "ADMIN_DISABLED") return ERROR_MESSAGES.ADMIN_DISABLED;
  if (error.code === "ADMIN_UNAUTHORIZED" || error.status === 401) return ERROR_MESSAGES.ADMIN_UNAUTHORIZED;
  return `无权访问管理接口（${error.code}）。`;
}

function acceptSnapshot(data) {
  if (!data || typeof data !== "object" || !data.pool) throw Object.assign(new Error("快照格式无效"), {code: "BAD_SNAPSHOT"});
  state.snapshot = data;
  state.lastOk = new Date();
  state.netError = "";
  state.authError = null;
}

async function poll(manual = false) {
  state.timer = null;
  if (!state.key || state.authError || document.hidden || state.polling) return;
  if (!manual && !state.autoRefresh) return;
  state.polling = true;
  $("refresh-now").classList.add("spinning");
  const generation = state.generation;
  try {
    const data = await adminFetch(ENDPOINT);
    if (generation === state.generation) acceptSnapshot(data);
  } catch (error) {
    if (generation !== state.generation) {
      /* key 已更换或已退出：丢弃旧请求的结果 */
    } else if (isAuthError(error)) {
      state.authError = authMessage(error);
      state.snapshot = null;
    } else {
      state.netError = `无法获取调度快照（${error.code || "NETWORK"}：${error.message}），${POLL_MS / 1000} 秒后重试。`;
    }
  } finally {
    state.polling = false;
    render();
    schedule(generation === state.generation ? POLL_MS : 0);
  }
}

function schedule(delay = POLL_MS) {
  clearTimeout(state.timer);
  state.timer = null;
  if (!state.key || state.authError || document.hidden || !state.autoRefresh) return;
  state.timer = setTimeout(poll, delay);
}

function restart() {
  state.generation += 1;
  if (state.controller) state.controller.abort();
  schedule(0);
}

function refreshNow() {
  if (!state.key || state.authError) return;
  clearTimeout(state.timer);
  state.timer = null;
  poll(true);
}

/* ---------- 交互 ---------- */

function readKey() {
  try { return sessionStorage.getItem(KEY_STORAGE) || ""; } catch { return ""; }
}

function storeKey(value) {
  try {
    if (value) sessionStorage.setItem(KEY_STORAGE, value); else sessionStorage.removeItem(KEY_STORAGE);
  } catch { /* 隐私模式下只保存在内存中 */ }
}

function onKeySubmit(event) {
  event.preventDefault();
  const value = $("key-input").value.trim();
  if (!value) { $("key-input").focus(); return; }
  state.key = value;
  storeKey(value);
  $("key-input").value = "";
  state.authError = null;
  state.netError = "";
  state.snapshot = null;
  render();
  /* 新 key 总是立即拉一次快照，即便自动刷新已暂停 */
  state.generation += 1;
  if (state.controller) state.controller.abort();
  clearTimeout(state.timer);
  state.timer = setTimeout(() => poll(true), 0);
}

function onKeyClear() {
  state.key = "";
  storeKey("");
  state.snapshot = null;
  state.authError = null;
  state.netError = "";
  state.formDirty = false;
  state.expanded.clear();
  state.lastOk = null;
  state.generation += 1;
  clearTimeout(state.timer);
  state.timer = null;
  if (state.controller) state.controller.abort();
  closeMenu();
  render();
  toast("admin key 已从本标签页清除。", "info", "已退出");
}

function collectSettings() {
  const s = state.snapshot;
  const body = {};
  const picked = document.querySelector("#policy-options input[name=policy]:checked");
  if (picked && picked.value !== s.policy) body.policy = picked.value;
  const cache = $("cache-wait");
  if (!cache.disabled && cache.value !== "") {
    const value = Number(cache.value);
    if (!Number.isFinite(value) || value < 0 || value > 600) {
      throw Object.assign(new Error("cache_wait_seconds 需在 0–600 之间。"), {code: "INVALID_INPUT"});
    }
    if (value !== num(s.cache_wait_seconds)) body.cache_wait_seconds = value;
  }
  return body;
}

function showSettingsError(error) {
  const box = $("settings-error");
  if (!error) { box.hidden = true; setText("settings-error-text", ""); return; }
  box.hidden = false;
  setText("settings-error-text", `${error.code || "ERROR"}：${ERROR_MESSAGES[error.code] && error.code !== "INVALID_INPUT" ? ERROR_MESSAGES[error.code] : error.message}`);
}

async function onSettingsSubmit(event) {
  event.preventDefault();
  if (!state.snapshot || state.applying) return;
  showSettingsError(null);
  let body;
  try { body = collectSettings(); } catch (error) { showSettingsError(error); return; }
  if (!Object.keys(body).length) {
    state.formDirty = false;
    render();
    toast("配置没有变化。", "info");
    return;
  }
  if (body.policy) {
    const lines = [
      {text: "排队中的请求会按原顺序迁移到新策略，不丢失、不重复；在途请求不受影响。"},
      {text: `${policyLabel(state.snapshot.policy)}  →  ${policyLabel(body.policy)}`, highlight: true},
    ];
    if (body.cache_wait_seconds !== undefined) lines.push({text: `同时修改 cache_wait_seconds 为 ${body.cache_wait_seconds} 秒。`});
    lines.push({text: "修改会持久化，并写入一条配置变更事件。"});
    const ok = await confirmDialog({
      title: "切换调度策略", description: "热切换 L4 调度策略", lines, confirmText: "确认切换",
    });
    if (!ok || !state.snapshot) return;
  }
  state.applying = true;
  render();
  try {
    const data = await adminFetch(SETTINGS_ENDPOINT, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    });
    acceptSnapshot(data);
    state.formDirty = false;
    const parts = [];
    if (body.policy) parts.push(`策略已切换为 ${data.policy}`);
    if (body.cache_wait_seconds !== undefined) parts.push(`cache_wait_seconds = ${num(data.cache_wait_seconds)}`);
    toast(`${parts.join("，")}。`, "success", "配置已应用");
  } catch (error) {
    if (isAuthError(error)) { state.authError = authMessage(error); state.snapshot = null; }
    showSettingsError(error);
    toast(error.message || "提交失败", "error", "配置未应用");
  } finally {
    state.applying = false;
    render();
    schedule();
  }
}

function onSettingsReset() {
  state.formDirty = false;
  showSettingsError(null);
  renderSettings(state.snapshot);
}

function onTenantClick(event) {
  const button = event.target.closest(".row-toggle");
  if (!button) return;
  const id = button.dataset.account;
  if (state.expanded.has(id)) state.expanded.delete(id); else state.expanded.add(id);
  renderTenants(state.snapshot);
  const again = [...document.querySelectorAll("#tenants-body .row-toggle")].find((b) => b.dataset.account === id);
  if (again) again.focus();
}

function markDirty() {
  state.formDirty = true;
  showSettingsError(null);
  renderSettings(state.snapshot);
}

function onAutoToggle() {
  state.autoRefresh = !state.autoRefresh;
  renderChrome(state.snapshot);
  if (state.autoRefresh) schedule(0); else { clearTimeout(state.timer); state.timer = null; }
  toast(state.autoRefresh ? `已恢复每 ${POLL_MS / 1000} 秒自动刷新。` : "已暂停自动刷新，可用刷新按钮手动更新。", "info");
}

/* ---------- 主题 ---------- */

function systemDark() {
  return Boolean(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
}

function applyTheme() {
  const mode = THEMES[state.theme] ? state.theme : "system";
  const dark = mode === "dark" || (mode === "system" && systemDark());
  document.documentElement.classList.toggle("dark", dark);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", dark ? "#191714" : "#faf9f5");
  for (const button of $$("[data-theme-toggle]")) {
    setIcon(button, THEMES[mode].icon);
    const text = `主题：${THEMES[mode].label}（点击切换为${THEMES[THEMES[mode].next].label}）`;
    button.title = text;
    button.setAttribute("aria-label", text);
  }
}

function onThemeToggle() {
  state.theme = THEMES[state.theme]?.next || "system";
  try { localStorage.setItem(THEME_STORAGE, state.theme); } catch { /* 存储不可用时只在本页生效 */ }
  applyTheme();
}

/* ---------- 导航 ---------- */

function closeMenu() {
  const menu = $("mobile-menu");
  if (menu.hidden) return;
  menu.hidden = true;
  const button = $("menu-toggle");
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-label", "打开导航菜单");
  setIcon(button, "menu");
}

function onMenuToggle() {
  const menu = $("mobile-menu");
  const open = menu.hidden;
  menu.hidden = !open;
  const button = $("menu-toggle");
  button.setAttribute("aria-expanded", String(open));
  button.setAttribute("aria-label", open ? "关闭导航菜单" : "打开导航菜单");
  setIcon(button, open ? "x" : "menu");
}

let navLock = 0;

function setActiveNav(id) {
  for (const link of $$("[data-nav]")) link.classList.toggle("active", link.dataset.nav === id);
}

function updateActiveNav() {
  if (Date.now() < navLock) return;
  const sections = $$("[data-section]").filter((el) => !el.hidden && el.offsetParent !== null);
  if (!sections.length) { setActiveNav(""); return; }
  const line = 120;
  let current = sections[0].dataset.section;
  let best = -Infinity;
  for (const el of sections) {
    const top = el.getBoundingClientRect().top;
    if (top <= line && top > best) { best = top; current = el.dataset.section; }
  }
  if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) {
    const bottom = sections.reduce((a, el) => (el.getBoundingClientRect().bottom > a.getBoundingClientRect().bottom ? el : a));
    if (bottom.getBoundingClientRect().top < window.innerHeight) current = bottom.dataset.section;
  }
  setActiveNav(current);
}

function onNavClick(event) {
  const link = event.target.closest("a[data-nav], a.brand");
  if (!link) return;
  const id = link.dataset.nav || "overview";
  const target = $(id);
  closeMenu();
  if (!target || target.hidden) { event.preventDefault(); return; }
  event.preventDefault();
  setActiveNav(id);
  navLock = Date.now() + 700;
  target.scrollIntoView({behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start"});
  try { history.replaceState(null, "", `#${id}`); } catch { /* 忽略 */ }
}

function init() {
  state.key = readKey();
  try { state.theme = localStorage.getItem(THEME_STORAGE) || "system"; } catch { state.theme = "system"; }
  applyTheme();
  if (window.matchMedia) {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => { if (state.theme === "system") applyTheme(); };
    if (media.addEventListener) media.addEventListener("change", onChange); else if (media.addListener) media.addListener(onChange);
  }
  $("key-form").addEventListener("submit", onKeySubmit);
  $("key-clear").addEventListener("click", onKeyClear);
  $("key-eye").addEventListener("click", () => {
    const input = $("key-input");
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    $("key-eye").setAttribute("aria-pressed", String(show));
    $("key-eye").setAttribute("aria-label", show ? "隐藏 key" : "显示 key");
    setIcon($("key-eye"), show ? "eye-off" : "eye");
  });
  for (const button of $$("[data-logout]")) button.addEventListener("click", onKeyClear);
  for (const sw of $$("[data-auto-toggle]")) sw.addEventListener("click", onAutoToggle);
  for (const button of $$("[data-theme-toggle]")) button.addEventListener("click", onThemeToggle);
  $("refresh-now").addEventListener("click", refreshNow);
  $("settings-form").addEventListener("submit", onSettingsSubmit);
  $("settings-reset").addEventListener("click", onSettingsReset);
  $("policy-options").addEventListener("change", markDirty);
  $("cache-wait").addEventListener("input", markDirty);
  $("tenants-body").addEventListener("click", onTenantClick);
  $("menu-toggle").addEventListener("click", onMenuToggle);
  document.addEventListener("click", onNavClick);
  for (const el of $$("[data-dialog-dismiss]")) el.addEventListener("click", () => closeDialog(false));
  $("dialog-confirm").addEventListener("click", () => closeDialog(true));
  document.addEventListener("keydown", (event) => {
    onDialogKey(event);
    if (event.key === "Escape") closeMenu();
  });
  let ticking = false;
  window.addEventListener("scroll", () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { ticking = false; updateActiveNav(); });
  }, {passive: true});
  window.addEventListener("resize", () => { if (window.innerWidth >= 1024) closeMenu(); });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearTimeout(state.timer);
      state.timer = null;
    } else {
      schedule(0);
    }
  });
  render();
  if (state.key) { state.timer = setTimeout(() => poll(true), 0); }
}

if (typeof document !== "undefined" && document.getElementById("main")) {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {shareModel, budgetParts, blockedLabel, eventLabel, formatSeconds, formatNumber, formatCompact, quotaTone, ratio, errorCode, errorText, POLICIES, BLOCKED};
}
