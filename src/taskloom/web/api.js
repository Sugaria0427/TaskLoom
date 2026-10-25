"use strict";

const $ = (id) => document.getElementById(id);
const shell = document.querySelector(".app-shell");
const state = {
  view: "chat", conversationId: null, selectedTaskId: null,
  conversations: [], tasks: [], events: [], resources: null, platform: null, status: null,
  after: 0, refreshing: false, submitting: false, archived: false, filter: "all",
  samples: [], drafts: new Map(), attachments: [], signatures: new Map(),
  inspector: true, error: "", toastTimer: null, ready: false,
};

function node(tag, className = "", text = "") {
  const result = document.createElement(tag);
  result.className = className;
  if (text !== "") result.textContent = text;
  return result;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(svg.namespaceURI, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function setText(id, value) { $(id).textContent = value; }
function number(value) { return Number(value || 0).toLocaleString("zh-CN"); }
function time(value) { return new Date(value).toLocaleTimeString("zh-CN", {hour12: false}); }
function bytes(value) {
  if (value >= 1048576) return `${(value / 1048576).toFixed(value % 1048576 ? 1 : 0)} MiB`;
  return `${(value / 1024).toFixed(value ? 1 : 0)} KiB`;
}
function elapsed(task) {
  if (!isActive(task) && !task.completed_at) return "—";
  const seconds = Math.max(0, ((task.completed_at ? Date.parse(task.completed_at) : Date.now()) - Date.parse(task.created_at)) / 1000);
  if (!Number.isFinite(seconds)) return "—";
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)} h`;
  if (seconds >= 60) return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
  return `${seconds.toFixed(1)} s`;
}
function isActive(task) { return task.outcome === "PENDING"; }
function sessionTasks() { return state.tasks.filter((task) => task.conversation_id === state.conversationId).sort((a, b) => a.created_at.localeCompare(b.created_at)); }
function conversation() { return state.conversations.find((item) => item.conversation_id === state.conversationId); }
function attempts(task) { return (state.resources?.attempts || []).filter((item) => item.root_task_id === task.task_id); }
function usageLabel() { return state.platform?.feedback?.usage_unit === "token_unit" ? "token 单位" : "模拟单位"; }
function priorityLabel(value) { return {high: "高", normal: "普通", low: "低"}[value] || "普通"; }
function phaseAge(task) {
  if (!isActive(task)) return "—";
  const pending = attempts(task).findLast((item) => item.outcome === "PENDING" || !item.quiescent);
  const event = state.events.findLast((item) => item.root_task_id === task.task_id && (!pending || item.attempt_id === pending.attempt_id));
  if (!event) return "—";
  const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(event.occurred_at)) / 1000));
  return Number.isFinite(seconds) ? seconds >= 60 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${seconds}s` : "—";
}
function priorityControl(task) {
  const label = node("label", "priority-control", "本地优先级");
  const select = node("select");
  select.setAttribute("aria-label", `${task.specification.title} 本地排队优先级`);
  for (const value of ["high", "normal", "low"]) {
    const option = node("option", "", priorityLabel(value)); option.value = value; select.append(option);
  }
  select.value = task.specification.priority || "normal";
  select.disabled = !isActive(task);
  select.onchange = async () => {
    const previous = task.specification.priority || "normal";
    select.disabled = true;
    try {
      const updated = await api(`/api/v1/tasks/${task.task_id}/priority`, {method: "POST", body: JSON.stringify({priority: select.value})});
      state.tasks = state.tasks.map((item) => item.task_id === task.task_id ? updated : item);
      toast("本地排队优先级已保存。"); render();
    } catch (error) { select.value = previous; toast(error.message); select.disabled = false; }
  };
  label.append(select);
  return label;
}
function currentTask() {
  const tasks = sessionTasks();
  return tasks.find((task) => task.task_id === state.selectedTaskId) || tasks.findLast(isActive) || tasks.at(-1);
}
function unchanged(key, data) {
  const signature = JSON.stringify(data);
  if (state.signatures.get(key) === signature) return true;
  state.signatures.set(key, signature);
  return false;
}

const errorMessages = {
  PLATFORM_UNAVAILABLE: "暂时无法连接 TaskLoom 平台，请检查平台服务与连接设置。",
  UNAUTHORIZED: "平台凭证无效，请重新连接平台账号。",
  CONNECTION_BUSY: "当前仍有执行中的任务，任务结束后可更新平台连接。",
  PLATFORM_ACCOUNT_CONFLICT: "此 profile 已绑定其他账号，请使用独立 profile 连接。",
  CONVERSATION_ARCHIVED: "请先恢复此会话，再发送新消息。",
  QUEUE_FULL: "本地任务或会话已达到容量上限。",
  ARTIFACT_BUDGET: "本地输入与产物空间不足，请检查资源预算。",
  LOCAL_HEADER_REQUIRED: "本地控制请求缺少必要标识，请刷新页面。",
  ACCOUNT_BUDGET: "平台账号额度已耗尽。",
  PRIVATE_BUDGET: "此次任务的额度预算不足。",
  DEADLINE_EXPIRED: "任务等待或执行时间已达到截止期限。",
  NOT_FOUND: "对应的会话或任务已不存在，请刷新页面。",
};

async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, {
      ...options, signal: controller.signal, cache: "no-store",
      headers: {"Content-Type": "application/json", "X-TaskLoom-Local": "1", ...options.headers},
    });
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(errorMessages[data.error?.code] || data.error?.message || "服务请求失败，请重试。");
      error.code = data.error?.code;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("服务响应超时，请先检查会话记录后再重试。");
    throw error;
  } finally { clearTimeout(timer); }
}

function toast(message) {
  clearTimeout(state.toastTimer);
  setText("toast", message);
  $("toast").hidden = false;
  state.toastTimer = setTimeout(() => $("toast").hidden = true, 3500);
}

function persistDraft() {
  const key = state.conversationId || "new";
  const draft = {text: $("message").value, attachments: [...state.attachments]};
  state.drafts.set(key, draft);
  try {
    localStorage.setItem("taskloom.draft", JSON.stringify({conversationId: state.conversationId, text: draft.text}));
  } catch { /* A full or disabled browser store does not affect server-side history. */ }
}

function restoreDraft() {
  const draft = state.drafts.get(state.conversationId || "new");
  $("message").value = draft?.text || "";
  state.attachments = [...(draft?.attachments || [])];
  renderAttachments();
  updateSend();
}

function closeDrawers() {
  shell.classList.remove("sidebar-open", "inspector-open");
  $("mobile-backdrop").hidden = true;
}

function navigate(view, id = state.conversationId, replace = false) {
  if (state.submitting && id !== state.conversationId) return;
  if (id !== state.conversationId) {
    persistDraft();
    state.conversationId = id;
    state.selectedTaskId = null;
    state.signatures.delete("messages");
    restoreDraft();
  }
  state.view = view;
  const hash = view === "monitor" ? "#monitor" : id ? `#chat/${id}` : "#chat";
  if (location.hash !== hash) history[replace ? "replaceState" : "pushState"](null, "", hash);
  closeDrawers();
  render();
}

function newConversation() {
  if (state.submitting) return;
  navigate("chat", null);
  $("message").focus();
}

function readRoute() {
  if (location.hash === "#monitor") navigate("monitor", state.conversationId, true);
  else if (location.hash.startsWith("#chat/")) navigate("chat", location.hash.slice(6), true);
  else if (location.hash !== "#main") navigate("chat", null, true);
}

function outcomeBadge(task) {
  const [label, kind] = {
    PENDING: ["进行中", "pending"], SUCCEEDED: ["已完成", "success"],
    FAILED: ["执行失败", "error"], CANCELLED: ["已取消", "neutral"], UNKNOWN: ["待核对", "warning"],
  }[task.outcome] || [task.outcome, "neutral"];
  return node("span", `badge ${kind}`, label);
}

function phase(task) {
  if (task.outcome === "SUCCEEDED") return "结果校验完成";
  if (task.outcome === "CANCELLED") return task.quiescent ? "执行已停止" : "正在确认退出";
  if (task.outcome === "FAILED") return "执行已结束";
  if (task.outcome === "UNKNOWN") return "正在核对执行结果";
  const pending = attempts(task).findLast((item) => item.outcome === "PENDING" || !item.quiescent);
  if (!pending) return "准备工作流";
  if (pending.state === "RECONCILING") return "核对执行结果";
  if (pending.kind === "tool") return pending.state === "RUNNING" ? "执行工具" : "等待工具资源";
  if (pending.state === "RUNNING") return "等待平台模型响应";
  const allocated = state.resources?.allocations?.some((item) => item.attempt_id === pending.attempt_id);
  return allocated ? "等待平台额度与窗口" : "本地模型请求排队";
}

function stepLabel(attempt, index) {
  if (attempt.kind === "tool") return "执行工具";
  return index === 0 ? "模型规划工具调用" : "模型生成统计结果";
}

function renderSidebar() {
  const query = $("session-search").value.trim().toLowerCase();
  if (unchanged("sidebar", [state.conversations, state.archived, state.conversationId, query])) return;
  const items = state.conversations.filter((item) => item.archived === state.archived && item.title.toLowerCase().includes(query));
  const list = $("session-list");
  const scroll = list.scrollTop;
  const children = [];
  let group = "";
  for (const item of items) {
    const date = new Date(item.updated_at);
    const today = new Date().toDateString() === date.toDateString();
    const name = state.archived ? "已归档" : today ? "今天" : "更早";
    if (group !== name) { children.push(node("p", "session-group", name)); group = name; }
    const button = node("button", `session-item${item.conversation_id === state.conversationId ? " selected" : ""}`);
    button.dataset.conversationId = item.conversation_id;
    button.setAttribute("aria-label", `打开会话：${item.title}`);
    if (item.conversation_id === state.conversationId) button.setAttribute("aria-current", "true");
    button.append(icon("chat"), node("span", "session-title", item.title));
    button.title = item.title;
    button.append(item.active_count ? node("span", "live-dot") : node("small", "", String(item.task_count)));
    button.onclick = () => navigate("chat", item.conversation_id);
    children.push(button);
  }
  if (!children.length) children.push(node("p", "sidebar-empty", query ? "没有匹配的会话。" : state.archived ? "暂无归档会话。" : "新建一次会话，记录每一步执行。"));
  list.replaceChildren(...children);
  list.scrollTop = scroll;
  $("show-archived").setAttribute("aria-pressed", String(state.archived));
  $("show-archived").setAttribute("aria-label", state.archived ? "查看进行中的会话" : "查看已归档会话");
  $("show-archived").title = state.archived ? "返回会话记录" : "查看已归档会话";
}

function renderHeader() {
  const monitor = state.view === "monitor";
  const current = conversation();
  setText("breadcrumb", monitor ? "个人端 L1—L3 · 平台 L4" : "个人工作台");
  setText("page-title", monitor ? "调度监控" : current?.title || "新会话");
  document.title = `${monitor ? "调度监控" : current?.title || "工作台"} · TaskLoom 织序`;
  $("chat-view").hidden = monitor;
  $("monitor-view").hidden = !monitor;
  shell.classList.toggle("monitor-mode", monitor);
  $("nav-chat").classList.toggle("active", !monitor);
  $("nav-monitor").classList.toggle("active", monitor);
  $("nav-chat").setAttribute("aria-current", monitor ? "false" : "page");
  $("nav-monitor").setAttribute("aria-current", monitor ? "page" : "false");
  $("rename-chat").hidden = !current || monitor;
  $("archive-chat").hidden = !current || monitor || current.archived;
  $("toggle-inspector").hidden = monitor;
  $("toggle-inspector").setAttribute("aria-expanded", String(window.innerWidth > 1020 ? state.inspector : shell.classList.contains("inspector-open")));
  $("archive-notice").hidden = !current?.archived;
  $("form").hidden = Boolean(current?.archived);
  $("welcome").hidden = sessionTasks().length > 0;
  const active = state.tasks.filter(isActive).length;
  setText("active-count", String(active));
  const tasks = sessionTasks();
  const running = tasks.filter(isActive).length;
  $("conversation-summary").hidden = !running;
  if (running) {
    $("conversation-summary").replaceChildren(node("span", "live-dot"), node("span", "", `${running} 项任务正在推进 · 可继续提交独立任务`));
  }
  updateSend();
}

function renderPlatform() {
  const platform = state.platform;
  const feedback = platform?.feedback;
  const connected = platform?.connected;
  setText("account", feedback?.account_id || "连接平台账号");
  setText("avatar", (feedback?.account_id || "T").slice(0,1).toUpperCase());
  setText("account-status", connected ? "TaskLoom 平台已连接" : "点击配置平台连接");
  setText("model-name", feedback?.model || "平台模型");
  setText("model-mode", feedback ? feedback.pool_kind === "simulated" ? "仿真" : "HTTP" : "离线");
  $("connection-dot").classList.toggle("offline", !connected);
  if (!$("platform-url").dataset.initialized && platform) {
    $("platform-url").value = platform.platform_url;
    $("platform-url").dataset.initialized = "1";
  }
  setText("settings-account", connected ? `已连接 ${feedback.account_id} · 剩余 ${number(feedback.account_remaining_units)} ${usageLabel()}` : "尚未连接平台账号，连接后即可提交任务。");
  setText("data-provenance", feedback?.pool_kind === "fixed-http" ? "指标来自真实调度账本；额度单位以平台计量为准。" : "模型池使用固定容量仿真；工具、进程和资源账本实际运行。额度以模拟单位计量。");
}

function executionDetails(task) {
  const details = node("details", "execution-details");
  details.dataset.taskId = task.task_id;
  const steps = attempts(task);
  const finished = steps.filter((item) => item.quiescent && item.outcome !== "PENDING").length;
  const summary = node("summary");
  summary.append(icon("bolt"), node("span", "", steps.length ? `${steps.length} 次执行尝试 · ${finished} 次已结束` : "等待工作流开始"), icon("chevron"));
  const list = node("div", "step-list");
  steps.forEach((item, index) => {
    const row = node("div", "step-row");
    row.append(icon(item.kind === "tool" ? "file" : "bolt"), node("span", "", stepLabel(item, index)), outcomeBadge(item), node("span", "step-cost", item.kind === "model" ? number(item.consumed_units) : "本地"));
    row.title = `${item.attempt_id} · ${item.state}`;
    list.append(row);
  });
  if (!steps.length) list.append(node("p", "empty-small", "任务已保存，调度器正在准备执行。"));
  details.append(summary, list);
  return details;
}

function taskButton(task, action, label, iconName) {
  const button = node("button", "", label);
  button.prepend(icon(iconName));
  button.onclick = () => taskAction(task.task_id, action, button);
  return button;
}

function renderMessage(task) {
  const turn = node("article", "message-turn");
  turn.dataset.taskId = task.task_id;
  const user = node("div", "message-user");
  const content = task.specification.instruction || Object.values(task.specification.documents).join("\n\n");
  user.append(node("pre", "", content));
  const files = node("div", "message-files");
  Object.keys(task.specification.documents).forEach((name) => {
    const file = node("span", "file-chip"); file.append(icon("file"), node("span", "", name)); files.append(file);
  });
  user.append(files);
  turn.append(user, node("div", "message-time", time(task.created_at)));
  const assistant = node("div", "message-assistant");
  const mark = node("div", "assistant-mark"); mark.append(icon("loom"));
  const body = node("div", "assistant-body");
  const head = node("div", "assistant-head"); head.append(node("strong", "", "TaskLoom"), outcomeBadge(task));
  body.append(head);
  if (task.result) {
    body.append(node("p", "assistant-text", task.result.summary || "文档统计已完成，结果已通过工具校验。"));
    const counts = node("div", "result-counts");
    for (const [label, value] of [["文件数量", task.result.files], ["词数统计", task.result.words]]) {
      const item = node("div"); item.append(node("span", "", label), node("strong", "", number(value))); counts.append(item);
    }
    body.append(counts);
  } else {
    body.append(node("p", "assistant-text", task.error ? errorMessages[task.error] || task.error : phase(task)));
  }
  body.append(executionDetails(task));
  const actions = node("div", "message-actions");
  actions.append(node("span", "", `消耗 ${number(task.consumed_units)} ${usageLabel()}${task.reserved_units ? ` · 预留 ${number(task.reserved_units)}` : ""}`));
  actions.append(priorityControl(task));
  if (isActive(task)) actions.append(taskButton(task, "cancel", "取消", "stop"));
  else if (task.outcome !== "SUCCEEDED") actions.append(taskButton(task, "resume", "核对并续作", "refresh"));
  if (task.result) {
    const link = node("a", "", "成果"); link.prepend(icon("download")); link.href = `/api/v1/artifacts/${task.task_id}`; link.target = "_blank"; link.rel = "noopener"; actions.append(link);
  }
  const inspect = node("button", "", "执行详情"); inspect.prepend(icon("panel"));
  inspect.onclick = () => { state.selectedTaskId = task.task_id; showInspector(true); renderInspector(); };
  actions.append(inspect, node("span", "task-reference", task.task_id.slice(0,8)));
  body.append(actions); assistant.append(mark, body); turn.append(assistant);
  return turn;
}

function renderMessages() {
  if (state.view !== "chat") return;
  const tasks = sessionTasks();
  if (unchanged("messages", [state.conversationId, tasks, tasks.map(attempts), state.resources?.allocations, usageLabel()])) return;
  const scroll = $("chat-scroll");
  const nearBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 100;
  const oldTop = scroll.scrollTop;
  const wasEmpty = !$("messages").childElementCount;
  const opened = new Set([...$("messages").querySelectorAll("details[open]")].map((item) => item.dataset.taskId));
  $("messages").replaceChildren(...tasks.map(renderMessage));
  $("messages").querySelectorAll("details").forEach((item) => item.open = opened.has(item.dataset.taskId));
  requestAnimationFrame(() => scroll.scrollTop = nearBottom || wasEmpty ? scroll.scrollHeight : oldTop);
}

function meter(label, used, total, value, note = "", color = "") {
  const container = node("div", `resource-meter ${color}`);
  const top = node("div", "meter-top"); top.append(node("span", "", label), node("strong", "", value));
  const track = node("div", "meter-track");
  const fill = node("span", "meter-fill"); fill.style.width = `${Math.min(100, Math.max(0, total ? used / total * 100 : 0))}%`;
  track.append(fill); container.append(top, track);
  if (note) container.append(node("p", "meter-note", note));
  return container;
}

function pair(label, value) {
  const result = node("div", "resource-pair"); result.append(node("span", "", label), node("strong", "", value)); return result;
}

function localMeters() {
  const r = state.resources;
  if (!r) return [node("p", "empty-small", "正在连接本地资源账本…")];
  const usedSlots = r.capacity.slots - r.available.slots;
  const usedMemory = r.capacity.memory_bytes - r.available.memory_bytes;
  const modelUsed = (r.model_capacity?.slots || 0) - (r.model_available?.slots || 0);
  return [
    meter("工具执行名额", usedSlots, r.capacity.slots, `${usedSlots} / ${r.capacity.slots}`, `等待工具资源：${r.queued_tools} 项`),
    meter("工具内存预留", usedMemory, r.capacity.memory_bytes, `${bytes(usedMemory)} / ${bytes(r.capacity.memory_bytes)}`, "预留量 / 调度预算"),
    meter("本地模型请求名额", modelUsed, r.model_capacity?.slots, `${modelUsed} / ${r.model_capacity?.slots ?? "—"}`, `本地请求排队：${r.queued_models} 项`, "blue"),
    meter("输入与成果空间", r.artifact_bytes, r.artifact_capacity_bytes, `${bytes(r.artifact_bytes)} / ${bytes(r.artifact_capacity_bytes || 0)}`, "实际文件总量 / 个人空间预算", "amber"),
  ];
}

function platformMeters() {
  const f = state.platform?.feedback;
  if (!f) return [node("p", "empty-small", "连接平台账号后，查看共享容量与账户额度。")];
  const signal = node("div", `platform-signal${f.can_submit ? "" : " waiting"}`);
  signal.append(icon(f.can_submit ? "check" : "clock"), node("span", "", !state.platform.connected ? "平台反馈暂不可用" : state.platform.coordinated ? f.can_submit ? "协同反馈已启用 · 可发起请求" : "协同反馈等待中" : "独立调度基线"));
  return [
    meter("共享模型池在途", f.pool_inflight, f.pool_capacity, `${f.pool_inflight} / ${f.pool_capacity}`, `平台全局排队：${f.platform_queued} 项`, "blue"),
    meter("账号额度已消耗", f.account_consumed_units, f.account_budget_units, number(f.account_consumed_units), `预算 ${number(f.account_budget_units)} ${usageLabel()}`),
    pair("当前预留", `${number(f.account_reserved_units)} ${usageLabel()}`),
    pair("可用额度", `${number(f.account_remaining_units)} ${usageLabel()}`),
    pair("账号在途 / 排队", `${f.account_inflight} / ${f.account_queued}`),
    pair("窗口剩余 Token", number(f.account_window_tokens_remaining)),
    signal,
  ];
}

function eventLabel(event) {
  const [kind, status] = event.type.split(".");
  const object = {task: "任务", tool: "工具", model: "模型请求"}[kind] || kind;
  const verb = {ready: "已创建", running: "开始执行", wait_resource: "等待资源", wait_quota: "等待额度", wait_dependency: "等待依赖", reconciling: "正在核对", finished: "已结束"}[status] || status;
  if (status === "finished") return `${object}${{SUCCEEDED: "已完成", FAILED: "执行失败", CANCELLED: "已取消", UNKNOWN: "待核对"}[event.outcome] || "已结束"}`;
  return `${object}${verb}`;
}

function renderInspector() {
  const tasks = sessionTasks();
  const consumed = tasks.reduce((sum, task) => sum + task.consumed_units, 0);
  const tokenUsage = tasks.flatMap(attempts).reduce((sum, attempt) => sum + (attempt.result?.usage?.input_tokens || 0) + (attempt.result?.usage?.output_tokens || 0), 0);
  setText("session-task-count", `${tasks.length} 项任务`);
  const stats = [];
  const tokenLabel = state.platform?.feedback?.pool_kind === "fixed-http" ? "累计 Token" : "模拟 Token";
  for (const [label, value, unit] of [["会话消耗", consumed, "单位"], [tokenLabel, tokenUsage, ""]]) {
    const item = node("div", "mini-stat");
    const strong = node("strong", "", number(value)); if (unit) strong.append(node("small", "", unit));
    item.append(node("span", "", label), strong); stats.push(item);
  }
  $("session-stats").replaceChildren(...stats);
  const task = currentTask();
  if (task) {
    const card = node("div", "execution-card");
    const head = node("div", "execution-card-head"); head.append(icon("file"), node("strong", "", task.specification.title));
    card.append(head, outcomeBadge(task));
    const track = node("div", "execution-track");
    const currentIds = new Set(Object.values(task.step_attempts));
    const steps = attempts(task).filter((attempt) => currentIds.has(attempt.attempt_id));
    const plan = [steps.find((a) => a.kind === "model"), steps.find((a) => a.kind === "tool"), steps.filter((a) => a.kind === "model").at(1)];
    plan.forEach((a) => track.append(node("span", `track-step${a?.outcome === "SUCCEEDED" ? " done" : a && !a.settled ? " running" : ""}`)));
    const caption = node("div", "execution-caption"); caption.append(node("span", "", phase(task)), node("span", "", elapsed(task)));
    card.append(track, caption); $("current-execution").replaceChildren(card);
    const allocations = (state.resources?.allocations || []).filter((item) => item.root_task_id === task.task_id);
    const memory = allocations.reduce((sum, item) => sum + item.resources.memory_bytes, 0);
    card.append(pair("当前工具内存预留", bytes(memory)));
    card.append(pair("任务额度上限", number(task.specification.budget_units)));
    card.append(pair("当前阶段持续", phaseAge(task)));
    card.append(node("p", "monitor-note", "资源数值为调度预留；模型消耗由平台结算，工具 CPU/RSS 尚未采集。"));
  } else $("current-execution").replaceChildren(node("p", "execution-empty", "发送第一条消息，查看模型请求与工具执行的完整过程。"));
  $("local-resources").replaceChildren(...localMeters());
  $("platform-resources").replaceChildren(...platformMeters());
  const ids = new Set(tasks.map((item) => item.task_id));
  const events = state.events.filter((event) => ids.has(event.root_task_id)).slice(-4).reverse();
  $("recent-events").replaceChildren(...(events.length ? events.map((event) => {
    const row = node("div", "recent-event");
    const content = node("div");
    const detail = node("p"); detail.append(node("span", "", event.root_task_id.slice(0,8)), node("time", "", time(event.occurred_at)));
    content.append(node("strong", "", eventLabel(event)), detail); row.append(node("span", "event-dot"), content); return row;
  }) : [node("p", "empty-small", "当前会话的执行事件会显示在这里。") ]));
}

function renderChart() {
  const samples = state.samples;
  const max = Math.max(1, ...samples.flatMap((item) => [item.tools, item.models]));
  const width = 500, height = 155, left = 25, bottom = 135, top = 10;
  const path = (key) => samples.map((item, index) => `${index ? "L" : "M"}${left + (width - left - 6) * index / Math.max(1, samples.length - 1)},${bottom - item[key] / max * (bottom - top)}`).join(" ");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `当前页面最近 ${samples.length} 次有效采样，工具排队 ${samples.at(-1)?.tools || 0} 项，模型排队 ${samples.at(-1)?.models || 0} 项`);
  const add = (tag, attrs, content = "") => {
    const item = document.createElementNS(svg.namespaceURI, tag);
    Object.entries(attrs).forEach(([key, value]) => item.setAttribute(key, value));
    item.textContent = content; svg.append(item);
  };
  [0, .5, 1].forEach((ratio) => {
    const y = bottom - ratio * (bottom - top);
    add("line", {x1: left, y1: y, x2: width - 4, y2: y, stroke: "#e9eee9", "stroke-dasharray": "4 5"});
    add("text", {x: 0, y: y + 3, fill: "#647365", "font-size": "11"}, String(Math.round(ratio * max * 10) / 10));
  });
  add("path", {d: path("tools"), fill: "none", stroke: "#729c7e", "stroke-width": 2});
  add("path", {d: path("models"), fill: "none", stroke: "#789ac1", "stroke-width": 2, "stroke-dasharray": "5 3"});
  const axis = node("div", "chart-axis"); axis.append(node("span", "", samples.length ? time(samples[0].at) : "—"), node("span", "", "最近 60 次采样"), node("span", "", "现在"));
  const caption = node("p", "chart-caption", "趋势从打开页面开始记录；刷新页面后重新采样。");
  $("queue-chart").replaceChildren(svg, axis, caption);
}

function renderMonitor() {
  if (state.view !== "monitor" || !state.resources) return;
  const active = state.tasks.filter(isActive).length;
  const finished = state.tasks.filter((task) => task.outcome === "SUCCEEDED");
  const durations = finished.map((task) => (Date.parse(task.completed_at) - Date.parse(task.created_at)) / 1000).filter((value) => Number.isFinite(value) && value >= 0);
  const average = durations.length ? durations.reduce((sum, value) => sum + value, 0) / durations.length : null;
  const r = state.resources, f = state.platform?.feedback;
  const cards = [
    ["进行中任务", String(active), `共 ${state.tasks.length} 项 · 已交付 ${finished.length} 项`, "bolt"],
    ["本地排队工作", String(r.queued_tools + r.queued_models), `工具 ${r.queued_tools} · 模型请求 ${r.queued_models}`, "clock"],
    ["平均任务用时", average === null ? "—" : `${average.toFixed(1)} s`, `${durations.length} 项完整时间记录 · 包含等待`, "check"],
    ["账号累计消耗", f ? number(f.account_consumed_units) : "—", `${usageLabel()} · 预留 ${number(f?.account_reserved_units)}`, "chip"],
  ];
  $("monitor-kpis").replaceChildren(...cards.map(([label, value, description, iconName]) => {
    const item = node("article", "kpi"); const title = node("div", "kpi-label"); title.append(node("span", "", label), icon(iconName)); item.append(title, node("strong", "", value), node("small", "", description)); return item;
  }));
  $("monitor-resources").replaceChildren(...localMeters());
  $("monitor-platform").replaceChildren(
    pair("共享模型池在途", f ? `${f.pool_inflight} / ${f.pool_capacity}` : "—"),
    pair("平台排队请求", f ? `${f.platform_queued} 项` : "—"),
    pair("账号在途 / 排队", f ? `${f.account_inflight} / ${f.account_queued}` : "—"),
    pair("账号可用额度", f ? `${number(f.account_remaining_units)} ${usageLabel()}` : "—"),
  );
  renderChart();
  if (!unchanged("table", [state.tasks, r.attempts, state.filter, Math.floor(Date.now() / 1000)])) {
    const tasks = [...state.tasks].filter((task) => state.filter === "all" || (state.filter === "active" ? isActive(task) : !isActive(task)));
    $("task-table").replaceChildren(...tasks.map((task) => {
      const row = node("tr");
      const title = node("td"); title.append(node("strong", "", task.specification.title), node("small", "", `${task.task_id.slice(0,12)} · 优先级 ${priorityLabel(task.specification.priority)}`));
      const status = node("td"); status.append(outcomeBadge(task));
      const allocation = (r.allocations || []).filter((item) => item.root_task_id === task.task_id);
      const assigned = allocation.map((item) => item.kind === "tool" ? `工具 ${item.resources.slots} · ${bytes(item.resources.memory_bytes)}` : `请求名额 ${item.resources.slots}`).join(" / ");
      const action = node("td");
      const view = node("button", "table-action", "查看"); view.append(icon("arrow-right"));
      view.onclick = () => { navigate("chat", task.conversation_id); state.selectedTaskId = task.task_id; renderInspector(); requestAnimationFrame(() => $("messages").querySelector(`[data-task-id="${task.task_id}"]`)?.scrollIntoView({block:"center"})); };
      action.append(view);
      row.append(title, status, node("td", "", `${phase(task)} · 阶段 ${phaseAge(task)}`), node("td", "", assigned || (isActive(task) ? "等待准入" : "已归还")), node("td", "mono", elapsed(task)), node("td", "mono", `${number(task.consumed_units)} / ${number(task.reserved_units)}`), action);
      return row;
    }));
    $("task-table-empty").hidden = tasks.length > 0;
  }
  const events = [...state.events].reverse();
  $("monitor-events").replaceChildren(...(events.length ? events.map((event) => {
    const row = node("div", "monitor-event"); row.append(node("time", "", time(event.occurred_at)), node("span", "", eventLabel(event)), node("code", "", event.root_task_id.slice(0,12))); return row;
  }) : [node("p", "empty-small", "提交任务后，执行事件将在这里实时显示。") ]));
}

function render() {
  renderSidebar(); renderHeader(); renderPlatform(); renderMessages(); renderInspector(); renderMonitor();
  setText("error", state.error);
  const r = state.resources;
  if (state.status) {
    setText("health", state.status.ready ? "本地服务运行中" : "本地服务准备中");
    setText("runtime-status", `${state.status.tenant_id} · ${state.status.protection === "cgroup-v2" ? "cgroup v2 资源隔离" : "进程组执行"}`);
  }
  if (r) setText("statusbar-queue", `排队  工具 ${r.queued_tools} · 模型 ${r.queued_models}`);
  setText("statusbar-feedback", state.platform?.connected ? state.platform.coordinated ? "平台反馈协同已启用" : "独立调度基线" : "平台未连接");
}

async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    const [status, resources, tasks, events, platform, conversations] = await Promise.all([
      api("/api/v1/status"), api("/api/v1/resources"), api("/api/v1/tasks"),
      api(`/api/v1/events?after=${state.after}`), api("/api/v1/platform"), api("/api/v1/conversations"),
    ]);
    Object.assign(state, {status, resources, tasks, platform, conversations, ready: true});
    if (events.length) { state.after = events.at(-1).event_seq; state.events = [...state.events, ...events].slice(-80); }
    state.samples.push({at: new Date().toISOString(), tools: resources.queued_tools, models: resources.queued_models});
    state.samples = state.samples.slice(-60);
    render();
  } catch (error) {
    setText("health", "本地连接待恢复");
    setText("statusbar-feedback", "连接中断 · 自动重连");
    $("connection-dot").classList.add("offline");
    state.ready = false;
    updateSend();
    if (!state.status) setText("error", error.message);
  } finally { state.refreshing = false; }
}

function updateSend() {
  $("submit").disabled = !state.ready || !state.platform?.connected || state.submitting || (!$("message").value.trim() && !state.attachments.length) || Boolean(conversation()?.archived);
  $("message").required = !state.attachments.length;
  $("new-chat").disabled = state.submitting;
  setText("workflow-note", state.platform?.connected ? "文档统计与结果校验 · 每条消息独立执行" : "连接平台账号后即可开始");
}

function renderAttachments() {
  $("attachments").replaceChildren(...state.attachments.map((file, index) => {
    const chip = node("span", "file-chip");
    const remove = node("button"); remove.type = "button"; remove.setAttribute("aria-label", `移除文件 ${file.name}`); remove.append(icon("close"));
    remove.onclick = () => { state.attachments.splice(index, 1); renderAttachments(); persistDraft(); updateSend(); };
    chip.append(icon("file"), node("span", "", `${file.name} · ${bytes(file.size)}`), remove); return chip;
  }));
}

async function addFiles(files) {
  const next = [...state.attachments];
  try {
    for (const file of files) {
      if (next.length >= 16) throw new Error("每次任务最多添加 16 份文本文件。");
      if (next.some((item) => item.name === file.name)) throw new Error(`文件名重复：${file.name}`);
      if (!/^[^/\\\x00-\x1f]{1,80}$/.test(file.name) || [".", ".."].includes(file.name)) throw new Error("文件名需要在 80 个字符以内，且不包含路径。");
      if (next.reduce((sum, item) => sum + item.size, 0) + file.size > 524288) throw new Error("每次任务的文本文件总量上限为 512 KiB。");
      let content;
      try { content = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer()); }
      catch { throw new Error(`${file.name} 不是有效的 UTF-8 文本文件。`); }
      if (content.includes("\u0000")) throw new Error(`${file.name} 含有二进制内容，请选择文本文件。`);
      next.push({name: file.name, content, size: new TextEncoder().encode(content).length});
    }
    state.attachments = next;
    state.error = "";
    renderAttachments(); persistDraft(); updateSend();
  } catch (error) { state.error = error.message; }
  setText("error", state.error);
  $("file-input").value = "";
}

async function sendMessage(event) {
  event.preventDefault();
  if (state.submitting || $("submit").disabled) return;
  const content = $("message").value.trim();
  const attachments = [...state.attachments];
  const iterations = Number($("iterations").value);
  const budget = Number($("task-budget").value);
  const title = content.replace(/\s+/g, " ").slice(0, 40) || `统计 ${attachments.length} 份文本资料`;
  const documents = attachments.length ? Object.fromEntries(attachments.map((item) => [item.name, item.content])) : {"notes.txt": content};
  state.submitting = true; state.error = ""; updateSend();
  try {
    if (!state.conversationId) {
      const created = await api("/api/v1/conversations", {method: "POST", body: JSON.stringify({title})});
      state.conversationId = created.conversation_id;
      state.conversations.unshift({...created, task_count: 0, active_count: 0});
      history.replaceState(null, "", `#chat/${created.conversation_id}`);
    }
    const task = await api(`/api/v1/conversations/${state.conversationId}/messages`, {method: "POST", body: JSON.stringify({
      title, instruction: content || "请统计所附文本文件并校验结果。", documents,
      work_iterations: iterations, budget_units: budget, priority: $("task-priority").value,
    })});
    if (!state.tasks.some((item) => item.task_id === task.task_id)) state.tasks.unshift(task);
    state.drafts.delete("new"); state.drafts.delete(state.conversationId);
    if ($("message").value.trim() === content) $("message").value = "";
    state.attachments = state.attachments.filter((item) => !attachments.includes(item));
    persistDraft(); renderAttachments();
    state.selectedTaskId = task.task_id;
    state.archived = false;
    state.signatures.delete("messages");
    render();
    await refresh();
    $("chat-scroll").scrollTop = $("chat-scroll").scrollHeight;
  } catch (error) { state.error = error.message; setText("error", state.error); }
  finally { state.submitting = false; updateSend(); }
}

async function taskAction(id, action, button) {
  button.disabled = true;
  try {
    const updated = await api(`/api/v1/tasks/${id}/${action}`, {method:"POST"});
    state.tasks = state.tasks.map((task) => task.task_id === id ? updated : task);
    toast(action === "cancel" ? "取消请求已保存，正在核对执行与结算。" : "任务正在核对并续作。");
    state.error = ""; render(); await refresh();
  } catch (error) { state.error = error.message; setText("error", state.error); button.disabled = false; }
}

async function updateConversation(update) {
  const updated = await api(`/api/v1/conversations/${state.conversationId}/update`, {method:"POST", body:JSON.stringify(update)});
  state.conversations = state.conversations.map((item) => item.conversation_id === updated.conversation_id ? {...item, ...updated} : item);
  render(); return updated;
}

function showInspector(show) {
  if (window.innerWidth <= 1020) {
    shell.classList.toggle("inspector-open", show);
    shell.classList.remove("sidebar-open");
    $("mobile-backdrop").hidden = !show;
  } else {
    state.inspector = show;
    shell.classList.toggle("inspector-hidden", !show);
    try { localStorage.setItem("taskloom.inspector", String(show)); } catch { /* Optional preference. */ }
  }
  $("toggle-inspector").setAttribute("aria-expanded", String(show));
}

async function connectAccount(action) {
  if (!$("platform-url").reportValidity()) return;
  if (action !== "connect" && (!$("username").reportValidity() || !$("password").reportValidity() || !$("username").value || !$("password").value)) {
    setText("connection-error", "请填写平台用户名与至少 10 个字符的密码。"); return;
  }
  if (action === "connect" && !$("api-key").value.trim()) { setText("connection-error", "请填写平台账号的 API Key。"); return; }
  const buttons = [$("login"), $("register"), $("connect")]; buttons.forEach((button) => button.disabled = true);
  const body = {platform_url: $("platform-url").value};
  if (action === "connect") body.api_key = $("api-key").value.trim();
  else Object.assign(body, {username: $("username").value, password: $("password").value});
  try {
    state.platform = await api(`/api/v1/platform/${action}`, {method:"POST", body:JSON.stringify(body)});
    $("password").value = ""; $("api-key").value = ""; setText("connection-error", "");
    await refresh(); $("settings-dialog").close(); toast("平台账号已连接。");
  } catch (error) { setText("connection-error", error.message); }
  finally { buttons.forEach((button) => button.disabled = false); }
}

$("new-chat").onclick = newConversation;
$("nav-chat").onclick = () => navigate("chat");
$("nav-monitor").onclick = () => navigate("monitor");
$("inspect-monitor").onclick = () => navigate("monitor");
document.querySelector(".brand").onclick = (event) => { event.preventDefault(); newConversation(); };
$("session-search").oninput = renderSidebar;
$("show-archived").onclick = () => { state.archived = !state.archived; renderSidebar(); };
$("form").onsubmit = sendMessage;
$("message").oninput = () => { persistDraft(); updateSend(); $("message").style.height = "auto"; $("message").style.height = `${Math.min(180, $("message").scrollHeight)}px`; };
$("message").onkeydown = (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); if (!$("submit").disabled) $("form").requestSubmit(); } };
$("attach-file").onclick = () => $("file-input").click();
$("file-input").onchange = () => addFiles([...$("file-input").files]);
$("task-options").onclick = () => { const open = $("options-panel").hidden; $("options-panel").hidden = !open; $("task-options").setAttribute("aria-expanded", String(open)); };
$("toggle-inspector").onclick = () => showInspector(window.innerWidth <= 1020 ? !shell.classList.contains("inspector-open") : !state.inspector);
$("close-inspector").onclick = () => { showInspector(false); $("toggle-inspector").focus(); };
$("toggle-sidebar").onclick = () => { const show = !shell.classList.contains("sidebar-open"); closeDrawers(); shell.classList.toggle("sidebar-open", show); $("mobile-backdrop").hidden = !show; };
$("mobile-backdrop").onclick = closeDrawers;
$("open-settings").onclick = () => { closeDrawers(); $("settings-dialog").showModal(); };
document.querySelectorAll("[data-close-dialog]").forEach((button) => button.onclick = () => $(button.dataset.closeDialog).close());
document.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("click", (event) => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } }));
$("login").onclick = () => connectAccount("login");
$("register").onclick = () => connectAccount("register");
$("platform-form").onsubmit = (event) => { event.preventDefault(); connectAccount("connect"); };
$("rename-chat").onclick = () => { $("conversation-title").value = conversation()?.title || ""; setText("rename-error", ""); $("rename-dialog").showModal(); $("conversation-title").select(); };
$("rename-form").onsubmit = async (event) => {
  event.preventDefault();
  const title = $("conversation-title").value.trim();
  if (!title) { setText("rename-error", "会话名称不能为空。"); return; }
  $("save-title").disabled = true;
  try { await updateConversation({title}); $("rename-dialog").close(); toast("会话名称已保存。"); }
  catch (error) { setText("rename-error", error.message); }
  finally { $("save-title").disabled = false; }
};
$("archive-chat").onclick = async () => { try { await updateConversation({archived:true}); toast("会话已归档，执行与成果记录已保留。"); } catch (error) { toast(error.message); } };
$("restore-chat").onclick = async () => { try { await updateConversation({archived:false}); state.archived = false; renderSidebar(); toast("会话已恢复。"); } catch (error) { toast(error.message); } };
$("task-filters").onclick = (event) => { const button = event.target.closest("button[data-filter]"); if (!button) return; state.filter = button.dataset.filter; $("task-filters").querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button)); renderMonitor(); };
document.querySelectorAll("[data-example]").forEach((button) => button.onclick = () => {
  const example = button.dataset.example;
  if (example === "files") { $("file-input").click(); return; }
  $("message").value = example === "notes" ? "Operating systems manage processes, memory, files and resources.\nTaskLoom coordinates local tools and model requests." : "Task scheduling coordinates independent work with limited resources.\nFair allocation protects short tasks while long tasks keep progressing.";
  $("iterations").value = example === "load" ? "5000000" : "1";
  if (example === "load") { $("options-panel").hidden = false; $("task-options").setAttribute("aria-expanded", "true"); }
  persistDraft(); updateSend(); $("message").focus();
});
window.addEventListener("popstate", readRoute);
window.addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); newConversation(); } if (event.key === "Escape") closeDrawers(); });
window.addEventListener("resize", () => { closeDrawers(); renderHeader(); });
window.addEventListener("beforeunload", persistDraft);
try {
  state.inspector = localStorage.getItem("taskloom.inspector") !== "false";
  const draft = JSON.parse(localStorage.getItem("taskloom.draft") || "null");
  if (draft?.text) state.drafts.set(draft.conversationId || "new", {text:draft.text, attachments:[]});
} catch { /* Server-side conversations remain available without browser storage. */ }
shell.classList.toggle("inspector-hidden", !state.inspector);
readRoute();
restoreDraft();
refresh();
setInterval(refresh, 1000);
