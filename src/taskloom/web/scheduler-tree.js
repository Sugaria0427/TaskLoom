/* Workflow canvas: DOM cards over SVG edges, stable viewport, explicit node inspection. */
(() => {
  "use strict";
  const Model = window.TaskLoomWorkflowModel;
  const $ = (id) => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  const view = {x:20,y:20,scale:1};
  const cards = new Map();
  let selectedTab = "resources", selectedNode = null, latest = null, graph = null, order = [];
  let mounted = false, initialized = false, expanded = false, edgeSignature = "", detailSignature = "";
  let visibilityKey = "", visibility = {hidden:[],included:[]};
  let selectedSessionId = "";
  const make = (tag,cls,text="") => { const el=document.createElement(tag); el.className=cls; el.textContent=text; return el; };
  const seconds = (value) => value == null || !Number.isFinite(value) ? "未记录" : value>=60 ? `${Math.floor(value/60)}分${Math.floor(value%60)}秒` : `${Math.max(0,value).toFixed(1)}秒`;
  const epoch = (value) => value ? Date.parse(value)/1000 : null;
  const clock = (value) => value ? new Date(value).toLocaleTimeString("zh-CN",{hour12:false}) : "未记录";
  const stateNames = {READY:"就绪",WAIT_DEPENDENCY:"等待依赖",WAIT_RESOURCE:"等待资源",WAIT_QUOTA:"等待额度",WAIT_APPROVAL:"等待确认",RUNNING:"执行中",RECONCILING:"核对中",FINISHED:"已结束"};
  const outcomes = {SUCCEEDED:"已完成",FAILED:"执行失败",CANCELLED:"已取消",UNKNOWN:"待核对"};
  const reasons = {cost_drr:"成本赤字轮转",deadline:"期限紧迫提升",aging:"等待老化提升",priority:"优先级提升",feasible_turn:"加权轮转与资源可行性检查",resources_released:"资源释放后的加权轮转候选",local_backpressure:"本地模型请求名额已满",feedback_unavailable:"等待平台反馈",stale_feedback:"等待更新平台反馈",platform_blocked:"平台反馈要求等待",platform_queued_full:"账号的平台队列已满",insufficient_budget:"账号预算不足",budget_reserved:"额度已被在途请求预留",window_exhausted:"等待 Token 窗口恢复",deadline_expired:"请求已到期限",no_candidate:"当前没有可派发候选"};
  const reason = (value) => reasons[value] || (value?.startsWith("short of ") ? "本地资源不足" : value || "等待调度器选择");
  const taskLabel = (task) => task.specification?.title || "Agent 任务";
  function title(node) {
    if (node.kind==="task") return taskLabel(node.task);
    if (node.kind==="prediction") return `预计下一项 · ${node.attempt.kind==="model" ? "模型请求" : "本地工具"}`;
    if (node.kind==="tool") return node.attempt.request_payload?.tool_name || "本地工具";
    const round=node.attempt.step_id.match(/-model-(\d+)$/);
    return round ? `第 ${Number(round[1])+1} 轮模型请求` : "模型请求";
  }
  function status(node) {
    if (node.kind==="prediction") return ["条件候选","prediction"];
    const item=node.attempt || node.task;
    if (item.outcome==="UNKNOWN" || item.state==="RECONCILING") return ["待核对","uncertain"];
    if (item.outcome!=="PENDING") return [outcomes[item.outcome] || "已结束",item.outcome==="SUCCEEDED"?"done":"failed"];
    if (node.queue) return [node.queue.blocked_by ? "阻塞等待" : "就绪排队","waiting"];
    if (item.state==="RUNNING") return [node.kind==="model" ? "已提交平台" : "执行中","running"];
    return [stateNames[item.state] || "就绪",item.state==="READY" ? "ready" : "waiting"];
  }
  function shortage(queue) {
    const missing=queue?.shortage || {}, parts=[];
    if (missing.slots) parts.push(`${missing.slots} 个工具名额`);
    if (missing.memory_bytes) parts.push(`${(missing.memory_bytes/1048576).toFixed(0)} MiB 内存`);
    if (missing.artifact_bytes) parts.push(`${(missing.artifact_bytes/1048576).toFixed(1)} MiB 产物空间`);
    return parts.length ? `尚缺 ${parts.join("、")}` : reason(queue?.blocked_by);
  }
  function timing(node) {
    const item=node.attempt || node.task, now=Date.now()/1000;
    if (node.kind==="task") return `累计 ${seconds((epoch(item.completed_at) ?? now)-epoch(item.created_at))}`;
    if (node.kind==="prediction") return reason(node.prediction.selection_reason);
    if (node.queue) return `等待 ${seconds(now-node.queue.enqueued_at)}`;
    if (item.dispatched_at) return `${item.kind==="model"?"提交后":"运行"} ${seconds((epoch(item.completed_at)??now)-epoch(item.dispatched_at))}`;
    return item.created_at ? `已揭示 ${seconds((epoch(item.completed_at)??now)-epoch(item.created_at))}` : "时间未记录";
  }
  function applyView() {
    $("workflow-world").style.transform=`translate(${view.x}px,${view.y}px) scale(${view.scale})`;
    $("workflow-zoom").textContent=`${Math.round(view.scale*100)}%`;
    const canvas=$("workflow-canvas");
    canvas.style.backgroundSize=`${24*view.scale}px ${24*view.scale}px`;
    canvas.style.backgroundPosition=`${view.x}px ${view.y}px`;
  }
  function zoom(factor,x,y) {
    const next=Math.max(.1,Math.min(2.5,view.scale*factor));
    const ratio=next/view.scale;
    view.x=x-(x-view.x)*ratio; view.y=y-(y-view.y)*ratio; view.scale=next; applyView();
  }
  function fit() {
    if (!graph?.nodes.length) return;
    const canvas=$("workflow-canvas"), w=canvas.clientWidth, h=canvas.clientHeight;
    if (!w || !h) return;
    view.scale=Math.max(.1,Math.min(1,(w-36)/graph.width,(h-36)/graph.height));
    view.x=(w-graph.width*view.scale)/2; view.y=(h-graph.height*view.scale)/2; applyView();
  }
  function centerNode(id) {
    const node=graph?.nodes.find((n) => n.id===id);
    if (!node) return;
    const canvas=$("workflow-canvas");
    view.scale=Math.max(.85,view.scale);
    view.x=canvas.clientWidth/2-(node.x+node.width/2)*view.scale;
    view.y=canvas.clientHeight/2-(node.y+node.height/2)*view.scale;
    applyView();
  }
  function readVisibility(data) {
    const key=`taskloom.workflowVisibility:${data.platform?.feedback?.platform_id || "local"}:${data.status?.tenant_id || "local"}`;
    if (visibilityKey === key) return;
    visibilityKey=key;visibility={hidden:[],included:[]};order=[];selectedNode=null;selectedSessionId="";
    try {
      const saved=JSON.parse(localStorage.getItem(key) || "null");
      if (saved && Array.isArray(saved.hidden) && Array.isArray(saved.included)) {
        visibility={hidden:saved.hidden.filter((id)=>typeof id==="string").slice(0,512),included:saved.included.filter((id)=>typeof id==="string").slice(0,512)};
      }
    } catch { /* Invalid or unavailable preferences use automatic visibility. */ }
  }
  function saveVisibility() {
    try {localStorage.setItem(visibilityKey,JSON.stringify(visibility));} catch { /* Optional preference. */ }
  }
  function selectedConversation() {return selectedSessionId;}
  function sessionWorkflows(id) {return graph?.workflows.filter((w)=>Model.conversationId(w.task)===id) || [];}
  function centerConversation(id) {
    const workflows=sessionWorkflows(id);
    const workflow=workflows.find((w)=>Model.active(w.task)) || workflows.at(-1);
    if (workflow) centerNode(`task:${workflow.id}`);
  }
  function sessionState(id) {
    const tasks=(latest?.tasks || []).filter((task)=>Model.conversationId(task)===id);
    const displayed=sessionWorkflows(id).length>0;
    const capacityBlocked=!displayed && tasks.length>0 && !tasks.some(Model.active) && graph.activeCount>=5;
    const hint=capacityBlocked ? "当前已有至少 5 项活跃任务；可先移出部分会话，再加入历史会话。" : displayed ? "仅移出画布，任务继续运行，执行记录保留。" : "优先展示该会话最近的一项任务及其活跃任务。";
    return {tasks,displayed,capacityBlocked,hint,active:tasks.some(Model.active)};
  }
  function sessionActions() {
    const id=selectedConversation();
    const {tasks,displayed,capacityBlocked,hint}=sessionState(id);
    $("workflow-rename-session").disabled=!id || !(latest?.conversations || []).some((c)=>c.conversation_id===id);
    $("workflow-locate-session").disabled=!displayed;
    $("workflow-session-note").textContent=!id ? "展开列表可直接加入或移出；选中后点击定位。" : !tasks.length ? "该会话尚无任务，可先重命名并开始对话。" : capacityBlocked ? hint : displayed ? "已选中会话；点击定位后再移动画布。" : "该会话未展示，可在列表行末加入画布。";
    const session=(latest?.conversations || []).find((c)=>c.conversation_id===id);
    $("workflow-picker-label").textContent=session?.title || "选择会话…";
  }
  function toggleConversation(id=selectedConversation()) {
    if (!id) return;
    const state=sessionState(id);
    if (!state.tasks.length || state.capacityBlocked) return;
    if (sessionWorkflows(id).length) {
      visibility.hidden=[id,...visibility.hidden.filter((value)=>value!==id)].slice(0,512);
      visibility.included=visibility.included.filter((value)=>value!==id);
    } else {
      visibility.hidden=visibility.hidden.filter((value)=>value!==id);
      visibility.included=[id,...visibility.included.filter((value)=>value!==id)].slice(0,512);
    }
    saveVisibility();render(latest);
  }
  function renderSessionList() {
    const sessions=[...(latest.conversations || [])];
    for (const task of latest.tasks || []) if (!sessions.some((c)=>c.conversation_id===Model.conversationId(task))) sessions.push({conversation_id:Model.conversationId(task),title:taskLabel(task)});
    const rows=sessions.map((session)=>({session,...sessionState(session.conversation_id)}));
    rows.sort((a,b)=>Number(b.active)-Number(a.active));
    if (selectedSessionId && !sessions.some((c)=>c.conversation_id===selectedSessionId)) selectedSessionId="";
    const list=$("workflow-session-list");
    const signature=JSON.stringify(rows.map((r)=>[r.session.conversation_id,r.session.title,r.session.archived,r.active,r.tasks.length,r.displayed,r.capacityBlocked,selectedSessionId]));
    if (list.dataset.signature!==signature) {
      const scroll=list.scrollTop;
      const focused=document.activeElement?.closest("[data-session-row]")?.dataset.sessionRow;
      const actionFocus=document.activeElement?.classList.contains("workflow-session-row-toggle");
      const elements=rows.map(({session,tasks,displayed,capacityBlocked,hint,active})=>{
        const id=session.conversation_id;
        const row=make("div",`workflow-session-row${selectedSessionId===id?" selected":""}`);row.dataset.sessionRow=id;
        const select=make("button","workflow-session-select");select.type="button";select.dataset.sessionId=id;
        select.setAttribute("aria-label",`选择会话：${session.title}`);select.setAttribute("aria-pressed",String(selectedSessionId===id));
        const name=make("strong","",session.title);name.title=session.title;
        const meta=make("span","workflow-session-row-meta");
        meta.append(make("span",`workflow-session-activity${active?" active":""}`,active?"活跃":tasks.length?"已结束":"暂无任务"),make("span","",displayed?"已展示":"未展示"));
        if (session.archived) meta.append(make("span","","已归档"));
        select.append(name,meta);
        select.onclick=()=>{selectedSessionId=id;sessionActions();renderSessionList();$("workflow-session-menu").open=false;$("workflow-picker").focus({preventScroll:true});};
        const toggle=make("button","workflow-session-row-toggle",displayed?"移出":"加入");toggle.type="button";
        toggle.setAttribute("aria-label",`${displayed?"移出画布":"加入画布"}：${session.title}`);
        toggle.disabled=!tasks.length || capacityBlocked;toggle.title=hint;
        toggle.onclick=(event)=>{event.stopPropagation();toggleConversation(id);};
        row.append(select,toggle);return row;
      });
      if (!elements.length) elements.push(make("p","workflow-session-list-empty","暂无会话，创建对话后可在这里管理。"));
      list.replaceChildren(...elements);list.dataset.signature=signature;list.scrollTop=scroll;
      if (focused) [...list.children].find((row)=>row.dataset.sessionRow===focused)?.querySelector(actionFocus?".workflow-session-row-toggle":".workflow-session-select")?.focus({preventScroll:true});
    }
    sessionActions();
  }
  function selectNode(id,focus=false) {
    selectedNode=id;
    for (const [key,card] of cards) { card.classList.toggle("selected",key===id); card.setAttribute("aria-pressed",String(key===id)); }
    renderDetails();
    if (focus) $("workflow-detail-title").focus({preventScroll:true});
  }
  function detailsRow(list,label,value) {
    list.append(make("dt","",label),make("dd","",String(value ?? "未记录")));
  }
  function renderDetails() {
    const node=graph?.nodes.find((n) => n.id===selectedNode);
    const panel=$("workflow-details");
    if (!node) { panel.hidden=true; detailSignature=""; return; }
    panel.hidden=false;
    const signature=JSON.stringify([node,Math.floor(Date.now()/1000)]);
    if (signature===detailSignature) return;
    detailSignature=signature;
    $("workflow-detail-title").textContent=title(node);
    const body=$("workflow-detail-body"), list=make("dl","workflow-facts");
    const scroll=body.scrollTop;
    const task=node.task, item=node.attempt || task;
    detailsRow(list,"会话",(latest.conversations || []).find((c) => c.conversation_id===task.conversation_id)?.title || taskLabel(task));
    detailsRow(list,"状态",status(node)[0]);
    detailsRow(list,"所属任务",task.task_id);
    detailsRow(list,"本地优先级",{high:"高",normal:"普通",low:"低"}[task.specification.priority] || "普通");
    if (node.kind==="prediction") {
      detailsRow(list,"选择依据",reason(node.prediction.selection_reason));
      detailsRow(list,"成立条件",node.prediction.conditional ? node.attempt.kind==="tool" ? "本地资源全部释放，队列保持不变。" : `${reason(node.prediction.blocked_by)}解除，队列与额度保持可行。` : "队列与资源保持当前状态。");
      detailsRow(list,"对应 Attempt",item.attempt_id);
    } else if (node.kind==="task") {
      detailsRow(list,"创建时间",clock(task.created_at)); detailsRow(list,"完成时间",clock(task.completed_at));
      detailsRow(list,"累计耗时",timing(node)); detailsRow(list,"已消耗额度",task.consumed_units || 0); detailsRow(list,"当前预留额度",task.reserved_units || 0);
    } else {
      detailsRow(list,"Attempt",item.attempt_id);
      detailsRow(list,"步骤建立",clock(item.created_at)); detailsRow(list,"本地派发",clock(item.dispatched_at)); detailsRow(list,"结果结算",clock(item.completed_at));
      if (item.created_at && item.dispatched_at) detailsRow(list,"本地等待",seconds(epoch(item.dispatched_at)-epoch(item.created_at)));
      if (node.queue) detailsRow(list,"当前等待",`${timing(node)}；${shortage(node.queue)}`);
      if (item.tool_claim) detailsRow(list,"资源申请",`${item.tool_claim.slots} 个名额；${(item.tool_claim.memory_bytes/1048576).toFixed(0)} MiB 内存预算`);
      if (node.queue?.cost_units!=null) {
        detailsRow(list,"估计成本",node.queue.cost_units); detailsRow(list,"轮转余额",node.queue.deficit_units);
      }
      const candidate=[latest.resources?.scheduling?.model,latest.resources?.scheduling?.tool].find((q) => q?.next_attempt_id===item.attempt_id);
      if (graph.fresh && candidate) detailsRow(list,"候选选择依据",reason(candidate.selection_reason));
      for (const [key,name] of [["created","平台入队"],["dispatched","平台派发"],["completed","平台完成"]]) {
        const value=item.execution_ref?.[`platform_${key}_at`]; if (value) detailsRow(list,name,clock(value));
      }
      const depNames=(item.predecessors || []).map((id) => {
        const prior=graph.nodes.find((n) => n.kind!=="prediction" && n.attempt?.step_id===id && n.task.task_id===task.task_id);
        return prior ? title(prior) : id;
      });
      detailsRow(list,"前置依赖",depNames.join("、") || "任务入口");
      if (node.unresolved) detailsRow(list,"依赖核对","前置步骤记录缺失或存在环，请检查原始执行图。");
    }
    if (item.error) detailsRow(list,"执行错误",item.error);
    body.replaceChildren(list);
    if (item.request_payload?.arguments && Object.keys(item.request_payload.arguments).length) {
      body.append(make("h4","","工具参数"),make("pre","workflow-arguments",JSON.stringify(item.request_payload.arguments,null,2).slice(0,6000)));
    }
    const note=make("p","workflow-detail-note",node.kind==="model" ? "本地派发表示提交平台；提交后时长包括平台排队与模型调用。" : node.kind==="prediction" ? "候选由本地调度队列只读预览产生；新请求、取消或资源变化会更新候选。" : "连线表示记录中的步骤依赖；资源数值为调度预算与预留。");
    body.append(note);
    if (task.conversation_id) {
      const link=make("a","workflow-open-chat","打开所属对话"); link.href=`#chat/${encodeURIComponent(task.conversation_id)}`; body.append(link);
    }
    body.scrollTop=scroll;
  }
  function edgePath(edge) {
    const a=edge.fromBox,b=edge.toBox;
    if (edge.kind==="prediction") {
      const startX=a.x+a.width,startY=a.y+a.height/2,endX=b.x,endY=b.y+b.height/2;
      if (endX>startX) return `M${startX},${startY} C${startX+25},${startY} ${endX-25},${endY} ${endX},${endY}`;
    }
    const sx=a.x+a.width/2,sy=a.y+a.height,tx=b.x+b.width/2,ty=b.y,mid=(sy+ty)/2;
    if (ty>sy) return `M${sx},${sy} C${sx},${mid} ${tx},${mid} ${tx},${ty}`;
    const outside=Math.max(a.x+a.width,b.x+b.width)+16;
    return `M${sx},${sy} C${outside},${sy+25} ${outside},${ty-25} ${tx},${ty}`;
  }
  function syncGraph() {
    const world=$("workflow-world"), svg=$("workflow-edges");
    world.style.width=`${graph.width}px`; world.style.height=`${graph.height}px`;
    svg.setAttribute("width",graph.width); svg.setAttribute("height",graph.height);
    const wanted=new Set(graph.nodes.map((n) => n.id));
    for (const [id,card] of cards) if (!wanted.has(id)) { card.remove(); cards.delete(id); }
    for (const node of graph.nodes) {
      let card=cards.get(node.id);
      if (!card) {
        card=make("button","workflow-node"); card.type="button"; card.dataset.nodeId=node.id;
        card.append(make("span","workflow-node-layer"),make("strong","workflow-node-title"),make("span","workflow-node-meta"),make("span","workflow-node-status"));
        card.onclick=() => selectNode(node.id);
        card.onfocus=() => { if (card.matches(":focus-visible")) centerNode(node.id); };
        $("workflow-nodes").append(card); cards.set(node.id,card);
      }
      const [label,tone]=status(node);
      card.className=`workflow-node ${tone}${selectedNode===node.id?" selected":""}`;
      card.style.left=`${node.x}px`; card.style.top=`${node.y}px`;
      card.setAttribute("aria-pressed",String(selectedNode===node.id));
      card.setAttribute("aria-label",`${title(node)}，${label}，${timing(node)}，点击查看详情`);
      card.children[0].textContent={task:"WORKFLOW · 根任务",model:"L3 · 模型请求",tool:"L1 · 工具执行",prediction:"调度器 · 只读预览"}[node.kind];
      card.children[1].textContent=title(node); card.children[1].title=title(node);
      card.children[2].textContent=timing(node); card.children[3].textContent=label;
    }
    const edgesKey=JSON.stringify([graph.frames.map((f) => [f.id,f.x,f.y,f.width,f.height,f.title,Model.active(f.task)]),graph.edges.map((e) => [e.from,e.to,e.kind,e.fromBox.x,e.fromBox.y,e.toBox.x,e.toBox.y])]);
    if (edgesKey!==edgeSignature) {
      edgeSignature=edgesKey;
      const frames=$("workflow-frames"); frames.replaceChildren();
      for (const frame of graph.frames) {
        const el=make("div",`workflow-frame${Model.active(frame.task)?" active":""}`);
        Object.assign(el.style,{left:`${frame.x}px`,top:`${frame.y}px`,width:`${frame.width}px`,height:`${frame.height}px`});
        el.append(make("span","workflow-frame-title",frame.title)); frames.append(el);
      }
      svg.replaceChildren();
      const defs=document.createElementNS(NS,"defs");
      for (const [id,color] of [["flow-arrow","#8ba899"],["flow-forecast","#7e9cce"]]) {
        const marker=document.createElementNS(NS,"marker");
        for (const [k,v] of Object.entries({id,viewBox:"0 0 10 10",refX:9,refY:5,markerWidth:6,markerHeight:6,orient:"auto-start-reverse"})) marker.setAttribute(k,v);
        const tip=document.createElementNS(NS,"path"); tip.setAttribute("d","M0 0 L10 5 L0 10 Z"); tip.setAttribute("fill",color); marker.append(tip); defs.append(marker);
      }
      svg.append(defs);
      for (const edge of graph.edges) {
        const path=document.createElementNS(NS,"path"); path.setAttribute("d",edgePath(edge)); path.setAttribute("class",`workflow-edge ${edge.kind}`);
        path.setAttribute("marker-end",`url(#${edge.kind==="prediction"?"flow-forecast":"flow-arrow"})`); svg.append(path);
      }
    }
    $("workflow-empty").hidden=graph.nodes.length>0;
    if (selectedNode && !wanted.has(selectedNode)) selectedNode=null;
    renderDetails();
  }
  function toggleExpand() {
    expanded=!expanded;
    $("inspector-panel-tree").classList.toggle("workflow-expanded",expanded);
    $("workflow-expand").textContent=expanded ? "收起画布" : "展开画布";
    $("workflow-expand").setAttribute("aria-expanded",String(expanded));
    if (!expanded) $("workflow-expand").focus({preventScroll:true});
  }
  function mount() {
    if (mounted) return;
    mounted=true;
    const canvas=$("workflow-canvas");
    canvas.addEventListener("wheel",(event) => {
      event.preventDefault();
      const rect=canvas.getBoundingClientRect(),delta=event.deltaY*(event.deltaMode===1?16:event.deltaMode===2?canvas.clientHeight:1);
      zoom(Math.exp(-Math.max(-300,Math.min(300,delta))*.002),event.clientX-rect.left,event.clientY-rect.top);
    },{passive:false});
    const pointers=new Map(); let pan=null,pinch=null;
    canvas.addEventListener("pointerdown",(event) => {
      if (event.button!==0 || event.target.closest(".workflow-node")) return;
      event.preventDefault(); canvas.focus({preventScroll:true}); canvas.setPointerCapture(event.pointerId);
      pointers.set(event.pointerId,{x:event.clientX,y:event.clientY});
      if (pointers.size===1) pan={x:event.clientX,y:event.clientY,vx:view.x,vy:view.y};
      if (pointers.size===2) {
        const [a,b]=[...pointers.values()],rect=canvas.getBoundingClientRect();
        const cx=(a.x+b.x)/2-rect.left,cy=(a.y+b.y)/2-rect.top;
        pinch={distance:Math.hypot(a.x-b.x,a.y-b.y),scale:view.scale,wx:(cx-view.x)/view.scale,wy:(cy-view.y)/view.scale}; pan=null;
      }
      canvas.classList.add("dragging");
    });
    canvas.addEventListener("pointermove",(event) => {
      if (!pointers.has(event.pointerId)) return;
      pointers.set(event.pointerId,{x:event.clientX,y:event.clientY});
      if (pointers.size===2 && pinch) {
        const [a,b]=[...pointers.values()],rect=canvas.getBoundingClientRect();
        view.scale=Math.max(.1,Math.min(2.5,pinch.scale*Math.hypot(a.x-b.x,a.y-b.y)/Math.max(1,pinch.distance)));
        view.x=(a.x+b.x)/2-rect.left-pinch.wx*view.scale; view.y=(a.y+b.y)/2-rect.top-pinch.wy*view.scale;
      } else if (pan) { view.x=pan.vx+event.clientX-pan.x; view.y=pan.vy+event.clientY-pan.y; }
      applyView();
    });
    const release=(event) => {
      pointers.delete(event.pointerId); pinch=null; pan=null;
      if (pointers.size===1) { const p=[...pointers.values()][0];pan={x:p.x,y:p.y,vx:view.x,vy:view.y}; }
      if (!pointers.size) canvas.classList.remove("dragging");
    };
    canvas.addEventListener("pointerup",release); canvas.addEventListener("pointercancel",release);
    canvas.addEventListener("lostpointercapture",release);
    canvas.addEventListener("keydown",(event) => {
      if (event.target!==canvas) return;
      const directions={ArrowLeft:[50,0],ArrowRight:[-50,0],ArrowUp:[0,50],ArrowDown:[0,-50]};
      if (directions[event.key]) { event.preventDefault(); view.x+=directions[event.key][0];view.y+=directions[event.key][1];applyView(); }
      if (["+","=","-"].includes(event.key)) { event.preventDefault();zoom(event.key==="-"?1/1.2:1.2,canvas.clientWidth/2,canvas.clientHeight/2); }
      if (event.key==="0") { event.preventDefault();fit(); }
    });
    $("workflow-zoom-in").onclick=()=>zoom(1.2,canvas.clientWidth/2,canvas.clientHeight/2);
    $("workflow-zoom-out").onclick=()=>zoom(1/1.2,canvas.clientWidth/2,canvas.clientHeight/2);
    $("workflow-zoom").onclick=()=>zoom(1/view.scale,canvas.clientWidth/2,canvas.clientHeight/2);
    $("workflow-fit").onclick=fit; $("workflow-expand").onclick=toggleExpand;
    $("workflow-detail-close").onclick=()=>{const previous=selectedNode;selectNode(null);cards.get(previous)?.focus({preventScroll:true});};
    $("workflow-focus").onclick=()=>centerNode(selectedNode || graph?.nodes.find((n)=>n.kind==="task" && Model.active(n.task))?.id || graph?.nodes[0]?.id);
    $("workflow-picker").onkeydown=(event)=>{
      if (event.key!=="ArrowDown") return;
      event.preventDefault();$("workflow-session-menu").open=true;$("workflow-session-list").querySelector("button:not(:disabled)")?.focus();
    };
    document.addEventListener("pointerdown",(event)=>{
      if (!$("workflow-session-menu").contains(event.target)) $("workflow-session-menu").open=false;
    });
    $("workflow-session-menu").addEventListener("keydown",(event)=>{
      if (event.key!=="Escape" || !$("workflow-session-menu").open) return;
      event.preventDefault();event.stopPropagation();$("workflow-session-menu").open=false;$("workflow-picker").focus({preventScroll:true});
    });
    $("workflow-locate-session").onclick=()=>centerConversation(selectedConversation());
    $("workflow-rename-session").onclick=()=>window.dispatchEvent(new CustomEvent("taskloom:rename-conversation",{detail:{conversationId:selectedConversation()}}));
    document.addEventListener("keydown",(event)=>{
      if (event.key!=="Escape") return;
      if (expanded) { event.preventDefault();event.stopPropagation();toggleExpand(); }
    });
    applyView();
  }
  function render(data) {
    latest=data;
    if (selectedTab!=="tree") return;
    mount();
    readVisibility(data);
    graph=Model.layout(Model.build(data,order,visibility)); order=graph.order;
    $("schedule-freshness").textContent=graph.fresh ? `已更新 ${clock(graph.observedAt)}` : "连接待恢复 · 保留最近状态";
    $("schedule-connection").classList.toggle("stale",!graph.fresh);
    $("schedule-count").textContent=`${graph.activeCount} 项活跃 · ${graph.workflows.length} 条 workflow`;
    syncGraph();
    renderSessionList();
    if (!initialized && graph.nodes.length && $("workflow-canvas").clientWidth) {
      initialized=true; view.scale=.9;view.x=20;view.y=20;applyView();
    }
  }
  window.TaskLoomTree={
    render,
    select(tab) {
      selectedTab=tab==="tree" ? "tree" : "resources";
      if (selectedTab!=="tree" && expanded) toggleExpand();
      for (const name of ["resources","tree"]) {
        const button=$(`inspector-tab-${name}`);button.setAttribute("aria-selected",String(name===selectedTab));button.tabIndex=name===selectedTab?0:-1;
        $(`inspector-panel-${name}`).hidden=name!==selectedTab;
      }
      document.querySelector(".app-shell").classList.toggle("schedule-tree-mode",selectedTab==="tree");
      try {localStorage.setItem("taskloom.inspectorTab",selectedTab);} catch { /* Optional preference. */ }
      if (latest) render(latest);
    },
  };
})();
