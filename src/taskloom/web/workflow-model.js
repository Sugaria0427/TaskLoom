/* Pure workflow projection and dependency layout; shared by the UI and Node checks. */
((scope) => {
  "use strict";
  const WIDTH = 220, HEIGHT = 102, GAP_X = 26, GAP_Y = 54, PAD = 24;
  const active = (task) => task.outcome === "PENDING" || task.outcome === "UNKNOWN" || task.quiescent === false;
  const timestamp = (value) => Date.parse(value || "") || 0;
  const conversationId = (task) => task.conversation_id || task.task_id;
  function selectTasks(tasks, visibility = {}) {
    const hidden = new Set(visibility.hidden || []);
    const available = tasks.filter((task) => !hidden.has(conversationId(task)));
    const ongoing = available.filter(active);
    const recent = available.filter((task) => !active(task)).sort((a,b) =>
      timestamp(b.completed_at || b.created_at) - timestamp(a.completed_at || a.created_at) || a.task_id.localeCompare(b.task_id));
    const preferred = [];
    // Manually added sessions get one recent workflow before automatic history fills
    // the remaining places. Active sessions already have their workflows on canvas.
    for (const id of visibility.included || []) {
      if (ongoing.some((task) => conversationId(task) === id)) continue;
      const latest = recent.find((task) => conversationId(task) === id);
      if (latest && !preferred.includes(latest)) preferred.push(latest);
    }
    return [...ongoing, ...[...preferred, ...recent.filter((task) => !preferred.includes(task))]
      .slice(0, Math.max(0, 5 - ongoing.length))];
  }
  function build(data, previousOrder = [], visibility = {}) {
    const tasks = selectTasks(data.tasks || [], visibility);
    const wanted = new Set(tasks.map((t) => t.task_id));
    const retained = previousOrder.filter((id) => wanted.has(id));
    for (const task of tasks) if (!retained.includes(task.task_id)) retained.push(task.task_id);
    const activeIds = new Set(tasks.filter(active).map((task) => task.task_id));
    // Stable partition: live work always occupies the front of the shared canvas,
    // while each group's previous order stays unchanged during ordinary refreshes.
    const order = [...retained.filter((id) => activeIds.has(id)), ...retained.filter((id) => !activeIds.has(id))];
    const observed = data.resources?.scheduling;
    const fresh = Boolean(data.ready && observed && Date.now() - timestamp(observed.observed_at) < 5000);
    const queues = fresh ? [observed.model, observed.tool].filter(Boolean) : [];
    const entries = new Map(queues.flatMap((q) => q.entries || []).map((e) => [e.attempt_id,e]));
    const conversations = new Map((data.conversations || []).map((c) => [c.conversation_id,c]));
    const allAttempts = data.resources?.attempts || [];
    const workflows = order.map((id) => {
      const task = tasks.find((item) => item.task_id === id);
      const current = new Set(Object.values(task.step_attempts || {}));
      const attempts = allAttempts.filter((a) => a.root_task_id === id && current.has(a.attempt_id)).sort((a,b) =>
        timestamp(a.created_at) - timestamp(b.created_at) || a.step_id.localeCompare(b.step_id,undefined,{numeric:true}));
      const root = {id:`task:${id}`,kind:"task",task,rank:0};
      const nodes = [root];
      const edges = [];
      const byStep = new Map(attempts.map((a) => [a.step_id,a]));
      const ranks = new Map();
      const unresolved = new Set(attempts);
      // Topological traversal uses only recorded predecessor facts. Missing/cyclic links
      // remain visible as unresolved, rather than inventing a sequential workflow.
      while (unresolved.size) {
        let progressed = false;
        for (const attempt of unresolved) {
          const deps = attempt.predecessors || [];
          if (!deps.every((p) => byStep.has(p) && ranks.has(p))) continue;
          ranks.set(attempt.step_id, Math.max(0,...deps.map((p) => ranks.get(p))) + 1);
          unresolved.delete(attempt); progressed = true;
        }
        if (!progressed) break;
      }
      for (const attempt of attempts) {
        const node = {id:`attempt:${attempt.attempt_id}`,kind:attempt.kind,task,attempt,
          queue:entries.get(attempt.attempt_id),rank:ranks.get(attempt.step_id) || 1,
          unresolved:unresolved.has(attempt)};
        nodes.push(node);
        if (!attempt.predecessors?.length) edges.push({from:root.id,to:node.id,kind:"entry"});
        for (const dependency of attempt.predecessors || []) {
          const prior = byStep.get(dependency);
          if (prior) edges.push({from:`attempt:${prior.attempt_id}`,to:node.id,kind:"dependency"});
        }
      }
      for (const queue of queues) {
        if (!queue.next_attempt_id) continue;
        const target = nodes.find((n) => n.attempt?.attempt_id === queue.next_attempt_id);
        if (!target) continue;
        const prediction = {id:`prediction:${target.attempt.attempt_id}`,kind:"prediction",task,
          attempt:target.attempt,queue:target.queue,prediction:queue,rank:target.rank};
        nodes.push(prediction);
        edges.push({from:target.id,to:prediction.id,kind:"prediction"});
      }
      return {id,task,title:conversations.get(task.conversation_id)?.title || task.specification.title,nodes,edges};
    });
    return {workflows,order,fresh,observedAt:observed?.observed_at,activeCount:tasks.filter(active).length};
  }
  function layout(model) {
    const blocks = [];
    // Every workflow has its own dependency layout, then shares the same world
    // coordinates in a two-column workspace. Live updates never change the camera.
    for (const workflow of model.workflows) {
      const levels = new Map();
      for (const node of workflow.nodes) {
        if (!levels.has(node.rank)) levels.set(node.rank,[]);
        levels.get(node.rank).push(node);
      }
      const columns = Math.min(3, Math.max(...[...levels.values()].map((list) => list.length)));
      const width = columns * WIDTH + (columns - 1) * GAP_X + PAD * 2;
      let y = 38;
      const nodes = [];
      const positions = new Map();
      for (const [,level] of [...levels].sort(([a],[b]) => a-b)) {
        for (let index=0; index<level.length; index++) {
          const band = Math.floor(index / 3), column = index % 3;
          const count = Math.min(3, level.length - band * 3);
          const x = PAD + (width - PAD*2 - (count*WIDTH + (count-1)*GAP_X))/2 + column*(WIDTH+GAP_X);
          const box = {...level[index],x,y:y+band*(HEIGHT+GAP_Y),width:WIDTH,height:HEIGHT};
          positions.set(box.id,box); nodes.push(box);
        }
        y += Math.ceil(level.length/3)*(HEIGHT+GAP_Y);
      }
      blocks.push({workflow,nodes,width,height:y-GAP_Y+PAD});
    }
    const nodes=[],edges=[],frames=[];
    const columnWidths=[0,0];
    blocks.forEach((block,index)=>{columnWidths[index%2]=Math.max(columnWidths[index%2],block.width);});
    let y=PAD;
    for (let index=0;index<blocks.length;index+=2) {
      const pair=blocks.slice(index,index+2);
      for (let column=0;column<pair.length;column++) {
        const block=pair[column],{workflow}=block,x=PAD+(column ? columnWidths[0]+36 : 0);
        const translated=block.nodes.map((node)=>({...node,x:node.x+x,y:node.y+y}));
        const positions=new Map(translated.map((node)=>[node.id,node]));nodes.push(...translated);
        frames.push({id:workflow.id,title:workflow.title,task:workflow.task,x,y,width:block.width,height:block.height});
        for (const edge of workflow.edges) {
          const from=positions.get(edge.from),to=positions.get(edge.to);
          if (from && to) edges.push({...edge,fromBox:from,toBox:to});
        }
      }
      y+=Math.max(...pair.map((b)=>b.height))+36;
    }
    return {...model,nodes,edges,frames,width:blocks.length ? PAD*2+columnWidths[0]+(blocks.length>1?36+columnWidths[1]:0) : 320,height:Math.max(220,y)};
  }
  const api = {active,selectTasks,build,layout,conversationId,WIDTH,HEIGHT};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else scope.TaskLoomWorkflowModel = api;
})(typeof window === "undefined" ? globalThis : window);
