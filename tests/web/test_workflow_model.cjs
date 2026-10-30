/* Run with: node tests/web/test_workflow_model.cjs */
const assert = require("node:assert/strict");
const model = require("../../src/taskloom/web/workflow-model.js");
const tasks = Array.from({length:9},(_,i)=>({
  task_id:`task-${i}`,conversation_id:`conversation-${i}`,
  specification:{title:`任务 ${i}`},outcome:"SUCCEEDED",quiescent:true,
  created_at:new Date(1700000000000+i*10000).toISOString(),
  completed_at:new Date(1700000000000+i*10000+1000).toISOString(),
  step_attempts:{},
}));
assert.deepEqual(model.selectTasks(tasks).map((t)=>t.task_id),["task-8","task-7","task-6","task-5","task-4"]);
const withActive=tasks.map((t,i)=>i<2?{...t,outcome:"PENDING",completed_at:null}:t);
assert.deepEqual(model.selectTasks(withActive).map((t)=>t.task_id),["task-0","task-1","task-8","task-7","task-6"]);
const allActive=tasks.map((t)=>({...t,outcome:"PENDING",completed_at:null}));
assert.equal(model.selectTasks(allActive).length,9);
assert.equal(model.selectTasks(allActive,{hidden:["conversation-0"]}).length,8);
assert.ok(!model.selectTasks(tasks,{hidden:["conversation-8"]}).some((t)=>t.task_id==="task-8"));
assert.equal(model.selectTasks(tasks,{included:["conversation-0"]})[0].task_id,"task-0");
assert.equal(model.selectTasks(tasks,{included:["conversation-0"]}).length,5);
assert.equal(model.selectTasks(allActive,{included:["conversation-0"]}).length,9);
assert.equal(model.selectTasks(tasks,{included:["conversation-0"],hidden:["conversation-0"]})[0].task_id,"task-8");
assert.deepEqual(model.selectTasks(withActive,{included:["conversation-2","conversation-3"]}).map((t)=>t.task_id),["task-0","task-1","task-2","task-3","task-8"]);
const reversedOldOrder=["task-8","task-7","task-6","task-1","task-0"];
const liveFirst=model.build({tasks:withActive,resources:{attempts:[]}},reversedOldOrder);
assert.deepEqual(liveFirst.order,["task-1","task-0","task-8","task-7","task-6"]);
assert.deepEqual(model.build({tasks:withActive,resources:{attempts:[]}},liveFirst.order).order,liveFirst.order);
const formerlyIdle=tasks.map((t,i)=>i===6?{...t,outcome:"PENDING",completed_at:null}:t);
assert.equal(model.build({tasks:formerlyIdle,resources:{attempts:[]}},["task-8","task-7","task-6"]).order[0],"task-6");
assert.equal(model.selectTasks([{...tasks[0],outcome:"UNKNOWN"},...allActive.slice(1)]).length,9);
assert.equal(model.selectTasks([{...tasks[0],quiescent:false},...allActive.slice(1)]).length,9);
const task={...tasks[0],outcome:"PENDING",completed_at:null,step_attempts:{m0:"a",t0:"b",t1:"c",m1:"d"}};
const attempts=[
  {attempt_id:"a",step_id:"m0",predecessors:[],kind:"model"},
  {attempt_id:"b",step_id:"t0",predecessors:["m0"],kind:"tool"},
  {attempt_id:"c",step_id:"t1",predecessors:["m0"],kind:"tool"},
  {attempt_id:"d",step_id:"m1",predecessors:["t0","t1"],kind:"model"},
].map((a)=>({...a,root_task_id:task.task_id,outcome:"PENDING",state:"WAIT_QUOTA"}));
const state={ready:true,tasks:[task],resources:{attempts,scheduling:{observed_at:new Date().toISOString(),
  model:{entries:[],next_attempt_id:"d",selection_reason:"cost_drr"},tool:{entries:[]}}}};
const graph=model.layout(model.build(state));
const byId=new Map(graph.nodes.map((n)=>[n.id,n]));
assert.equal(byId.get("attempt:b").y,byId.get("attempt:c").y);
assert.ok(byId.get("attempt:a").y<byId.get("attempt:b").y);
assert.ok(byId.get("attempt:c").y<byId.get("attempt:d").y);
assert.equal(graph.edges.filter((e)=>e.kind==="dependency").length,4);
assert.equal(graph.nodes.filter((n)=>n.kind==="prediction").length,1);
for (let i=0;i<graph.nodes.length;i++) for (let j=i+1;j<graph.nodes.length;j++) {
  const a=graph.nodes[i],b=graph.nodes[j];
  assert.ok(a.x+a.width<=b.x || b.x+b.width<=a.x || a.y+a.height<=b.y || b.y+b.height<=a.y,"nodes overlap");
}
assert.deepEqual(model.build(state,graph.order).order,graph.order);
const disconnected=model.build({...state,ready:false});
assert.equal(disconnected.workflows[0].nodes.filter((n)=>n.kind==="prediction").length,0);
assert.equal(disconnected.workflows[0].nodes.filter((n)=>n.attempt).length,4);
const cyclic={...state,resources:{attempts:[{...attempts[0],predecessors:["m1"]},...attempts.slice(1)]}};
assert.equal(model.build(cyclic).workflows[0].nodes.filter((n)=>n.unresolved).length,4);
console.log("Workflow model: retention, unlimited active tasks, fan-out/join, previews, layout and cycles passed.");
