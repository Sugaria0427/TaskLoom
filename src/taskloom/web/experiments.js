"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const labels = {fifo: "FIFO", round_robin: "平台轮转", taskloom: "TaskLoom"};
  let current = null, polling = null, loading = false, generation = 0;
  const elem = (tag, text = "", className = "") => {
    const node = document.createElement(tag); node.textContent = text; node.className = className; return node;
  };
  const fmt = (value, digits = 1) => value == null ? "—" : Number(value).toLocaleString("zh-CN", {maximumFractionDigits: digits, minimumFractionDigits: digits});
  const metric = (row, key) => row.metrics[key]?.mean;
  function showError(message) { $("error").hidden = !message; $("error").textContent = message || ""; }

  async function request(path, options = {}) {
    const response = await fetch(path, {...options, cache: "no-store", headers: {"Content-Type": "application/json", "X-TaskLoom-Local": "1"}});
    const data = await response.json();
    if (!response.ok) {
      const message = data.error?.message || (response.status === 422 ? "参数超出范围；天数 × 每日任务 × 重复数最多为 18000。" : `请求失败：${response.status}`);
      throw new Error(message);
    }
    return data;
  }

  function busy(value) {
    loading = value; $("run").disabled = value;
    $("run").textContent = value ? "正在计算…" : "运行对照实验";
    $("progress").hidden = !value;
    $("report").hidden = value || !current;
    $("empty").hidden = value || Boolean(current);
    $("history").disabled = value;
  }

  async function history(selected = "") {
    const items = await request("/api/v1/experiments");
    $("history").replaceChildren();
    if (!items.length) $("history").append(new Option("暂无记录", ""));
    for (const item of items) {
      const title = `${new Date(item.created_at).toLocaleString("zh-CN", {month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit"})} · ${item.spec.days} 天 · ${item.status === "FINISHED" ? "完成" : item.status === "RUNNING" ? "计算中" : "中断"}`;
      $("history").append(new Option(title, item.run_id));
    }
    if (selected) $("history").value = selected;
    return items;
  }

  async function load(runId) {
    clearTimeout(polling);
    const ticket = ++generation;
    try {
      const record = await request(`/api/v1/experiments/${encodeURIComponent(runId)}`);
      if (ticket !== generation) return;
      if (record.status === "RUNNING") {
        busy(true); $("empty").hidden = true;
        polling = setTimeout(() => load(runId), 1000);
      } else {
        busy(false);
        if (record.status === "FAILED") { showError(record.error); return; }
        current = record; render(); await history(runId);
      }
    } catch (error) { if (ticket === generation) { busy(false); showError(error.message); } }
  }

  function render() {
    if (!current?.result) return;
    const result = current.result, spec = result.spec;
    $("empty").hidden = true; $("report").hidden = false;
    $("report-title").textContent = `${spec.days} 天运行，${spec.repeats} 组配对种子`;
    $("report-meta").textContent = `每组 ${spec.days * spec.tasks_per_day} 项任务 · ${spec.users} 名用户 · 模型池 ${spec.model_slots} 槽 · 每人 ${spec.tool_slots} 工具槽 / ${spec.memory_mib} MiB · 种子 ${spec.seed}–${spec.seed + spec.repeats - 1}${current.calculation_seconds == null ? "" : ` · 计算 ${fmt(current.calculation_seconds)} 秒`}`;
    $("export").href = `/api/v1/experiments/${encodeURIComponent(current.run_id)}/export`;
    const baseline = $("baseline").value, diff = result.improvements_percent[baseline];
    const optimized = result.summary.find(row => row.policy === "taskloom");
    const cards = [
      ["交互平均周转", "interactive_mean_seconds", `${fmt(metric(optimized,"interactive_mean_seconds"))} 秒`],
      ["交互 P95 周转", "interactive_p95_seconds", `${fmt(metric(optimized,"interactive_p95_seconds"))} 秒`],
      ["按时完成率", "on_time_percentage_points", `${fmt(metric(optimized,"on_time_rate") * 100)}%`],
      ["整体平均周转", "mean_turnaround_seconds", `${fmt(metric(optimized,"mean_turnaround_seconds"))} 秒`],
    ];
    $("kpis").replaceChildren(...cards.map(([title,key,value]) => {
      const card=elem("article","","kpi"), change=diff[key];
      card.append(elem("h3",title),elem("strong",value));
      const suffix=key === "on_time_percentage_points" ? " 个百分点" : "%";
      const meaning=change == null ? "基线为零，改善比例不可计算" : `较${labels[baseline]} ${change >= 0 ? "改善" : "变差"} ${fmt(Math.abs(change))}${suffix}`;
      card.append(elem("p",meaning,change >= 0 ? "good" : "bad"));return card;
    }));
    $("summary").replaceChildren(...result.summary.map(row => {
      const tr=elem("tr","",row.policy === "taskloom" ? "optimized-row" : "");tr.append(elem("td",labels[row.policy]));
      for (const key of ["interactive_mean_seconds","interactive_p95_seconds","on_time_rate","mean_turnaround_seconds","background_max_wait_seconds"]) {
        const data=row.metrics[key], factor=key === "on_time_rate" ? 100 : 1, unit=key === "on_time_rate" ? "%" : "";
        const text=data.mean == null ? "—" : `${fmt(data.mean*factor)}${unit}${spec.repeats > 1 ? ` ± ${fmt(data.stddev*factor)}` : ""}`;
        tr.append(elem("td",text));
      }
      return tr;
    }));
    $("conservation").textContent = `每组均完成 ${fmt(metric(optimized,"completed_tasks"),0)} 项任务；服务工作量为 ${fmt(metric(optimized,"total_service_seconds"))} 秒。模型池全时段利用率 ${fmt(metric(optimized,"model_utilization")*100,2)}%（含夜间空闲）。`;
    $("assumptions").replaceChildren(...result.assumptions.map(text => elem("li",text)));
    chart(); trace();
  }

  function chart() {
    if (!current) return;
    const rows=current.result.summary, width=Math.max(350,$("daily-chart").clientWidth), height=220;
    const left=45,right=18,top=12,bottom=30;
    const values=rows.flatMap(row=>row.daily.map(day=>day.interactive_mean_seconds).filter(v=>v != null));
    const ceiling=Math.max(1,...values)*1.1, days=current.spec.days;
    const svg=document.createElementNS("http://www.w3.org/2000/svg","svg");svg.setAttribute("viewBox",`0 0 ${width} ${height}`);
    const add=(tag,attributes,text)=>{const node=document.createElementNS(svg.namespaceURI,tag);for(const [k,v] of Object.entries(attributes))node.setAttribute(k,String(v));if(text!==undefined)node.textContent=text;svg.append(node);return node;};
    const x=(day)=>days===1 ? (left+width-right)/2 : left+(day-1)/(days-1)*(width-left-right);
    const y=(value)=>top+(1-value/ceiling)*(height-top-bottom);
    for(let i=0;i<=4;i++){const value=ceiling*i/4;add("line",{x1:left,y1:y(value),x2:width-right,y2:y(value),stroke:"#e7eee9"});add("text",{x:left-8,y:y(value)+4,"text-anchor":"end"},fmt(value,0));}
    const ticks=[...new Set([1,Math.ceil(days/3),Math.ceil(days*2/3),days])];
    for(const day of ticks)add("text",{x:x(day),y:height-7,"text-anchor":"middle"},`第 ${day} 天`);
    const colors={fifo:"#acb5c0",round_robin:"#9172be",taskloom:"#328565"};
    for(const row of rows){const points=row.daily.filter(day=>day.interactive_mean_seconds!=null);add("polyline",{points:points.map(day=>`${x(day.day)},${y(day.interactive_mean_seconds)}`).join(" "),fill:"none",stroke:colors[row.policy],"stroke-width":2.5});for(const day of points)add("circle",{cx:x(day.day),cy:y(day.interactive_mean_seconds),r:3,fill:colors[row.policy]});}
    $("daily-chart").replaceChildren(svg);
  }

  function trace() {
    if (!current) return;
    const run=current.result.runs.find(item=>item.policy === $("trace-policy").value);
    const names={model_queued:"模型请求排队",model_started:"模型请求派发",model_finished:"模型请求完成",tool_queued:"工具等待资源",tool_started:"工具获准执行",tool_finished:"工具完成回收",task_finished:"根任务完成"};
    $("trace-note").textContent=`首组种子 ${run.seed} · 展示最早 ${run.trace.length} / ${run.trace_total_events} 条事件。完整导出包含所有种子的指标及各组最多 ${current.result.trace_limit_per_run} 条事件样本。`;
    $("trace").replaceChildren(...run.trace.map(event=>{
      const row=elem("tr");for(const text of [fmt(event.at_seconds,2),names[event.type]||event.type,`${event.user} / ${event.task_id}`,String(event.model_inflight),`${event.local_tool_inflight} / ${event.local_memory_mib} MiB`])row.append(elem("td",text));return row;
    }));
  }

  $("experiment-form").addEventListener("submit",async event=>{
    event.preventDefault();if(loading)return;showError("");busy(true);
    try{const spec=Object.fromEntries([...new FormData(event.currentTarget)].map(([key,value])=>[key,Number(value)]));const record=await request("/api/v1/experiments",{method:"POST",body:JSON.stringify(spec)});await history(record.run_id);await load(record.run_id);}
    catch(error){busy(false);showError(error.message);}
  });
  $("baseline").addEventListener("change",render);$("trace-policy").addEventListener("change",trace);
  $("history").addEventListener("change",()=>{if($("history").value){showError("");load($("history").value);}});
  window.addEventListener("resize",chart);
  history().then(items=>{if(items.length)load(items[0].run_id);}).catch(error=>showError(error.message));
})();
