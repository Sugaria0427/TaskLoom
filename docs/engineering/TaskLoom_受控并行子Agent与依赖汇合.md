# 受控并行子 Agent 与依赖汇合

工作包：`[L2] 实现受控并行子 Agent、依赖汇合与取消恢复`（Issue IKR9I1）。
接入位置：`contracts/models.py`、`runtime/agent.py`、`client/service.py`、`domain/model.py`（`Agent.label`）。

## 目标

让一个根任务拆成受控的并行分支（例如同时解析资料、核对来源），必要结果齐备后由父 Agent 汇合为最终成果。分支仅用于非对话（`legacy`）任务：`mode="chat"` 携带 `branches` 时契约校验拒绝；不带 `branches` 的任务行为不变。

## 契约

- `TaskCreate.branches: list[BranchSpec]`（最多 8 个，有界分支数）。
- `BranchSpec`：`agent_id`（任务内分支标签）、`instruction`（分支指令）、`required`（必需/可选）、`depends_on`（依赖的分支标签，全部 SUCCEEDED 后才启动）。校验唯一性、依赖存在、无自依赖、无环（Kahn 剥离）。
- 内部身份 `branch_agent_id(task_id, label) = "{task_id}:{label}"`：agent/step 记录、attempt 与 `ModelRequest.agent_id` 都使用它，不同任务可复用同名标签而不互相覆盖。
- `TaskState.branches: dict[str, BranchState]`（按标签索引）：分支的持久化运行时状态（独立 `checkpoint`、`state`/`outcome`、`result`、`error`），随任务记录一并落盘。`quiescent`/`consumed_units`/`reserved_units` 在每次保存 attempt 时由该分支的 attempt 汇总得出；`cancel_generation` 记录分支最近运行所在的根任务代次。
- 分支 Agent 身份复用 `domain/model.py::Agent(agent_id, root_task_id, parent_agent_id, label)`；创建分支时落一条带 `parent_agent_id` 与 `label` 的 agent 记录，`/tasks/{id}/graph` 可见。

## 接入路径

| 层 | 改动 |
|---|---|
| `contracts/models.py` | 新增 `BranchSpec`、`BranchState`、`branch_agent_id`；`TaskCreate.branches`（唯一性/依赖存在/自依赖/环/chat 拒绝）；`TaskState.branches`。 |
| `runtime/agent.py` | `run_agent` 增加 `agent_id`、`instruction`、`branch_results` 可选参数：步骤身份与初始化提示按分支隔离；父汇总的 `branch_results` 与原 system 提示、原 instruction 写在同一条初始 user 请求中，仅在检查点为空时写入，续作不会重复注入。 |
| `client/service.py` | `_run` 检测 `specification.branches` 后进入 `_run_with_branches`：初始化或重置分支状态 → `_await_branches` 按依赖启动 `_run_branch` 并行 runner → 等待汇合 → 父 `run_agent(branch_results=...)` 做最终汇总。新增 `BranchHost`（分支的 model/tool/checkpoint 适配，全部携带根任务身份）。`_attempt`/`model`/`tool` 接受 `branch` 标签，分支步骤身份与依赖检查在分支自己的 checkpoint 空间内完成。 |

## 执行模型

- 父任务在分支运行期间置 `WAIT_DEPENDENCY`，**不发起任何模型/工具请求**——把执行许可安全让出给分支；汇合后回到 `RUNNING` 由父 Agent 汇总。
- 每个分支运行自己的有界模型—工具循环（独立 `Checkpoint`、独立步骤身份、独立 `max_steps`）。
- 依赖汇合：`depends_on` 中所有分支 SUCCEEDED 才启动后继分支；依赖已结束但未成功时，必需分支直接判死（`BRANCH_DEPENDENCY_FAILED`），可选分支不启动并标记 FAILED。若既无在途分支也无可启动分支（绕过校验的环），根任务以 `BRANCH_DEADLOCK` 结束，不空转。
- 失败策略（快速失败）：必需分支一旦以非成功结束，立即抛 `BRANCH_FAILED`（未核对时为 `BRANCH_UNRECONCILED`），`finally` 级联取消其余在途分支并等待其停止；仅可选分支干净失败 → 父用成功分支结果继续。
- 结果未知：分支结果按其 attempt 推导——存在未静止或 UNKNOWN 未结算的 attempt 时分支记 UNKNOWN/RECONCILING，而非 FAILED。任一分支 UNKNOWN（无论必需与否）都不开始父汇总，根任务以 `BRANCH_UNRECONCILED` 进入 UNKNOWN/RECONCILING，等待核对后续作。

## 预算与资源（分支不额外获得份额）

- 所有分支模型请求 `ModelRequest.root_task_id = 根任务`：L3 本地调度与 L4 平台准入都按根任务计费与限流，分支数量不扩大根预算或公平份额。
- 分支工具请求走同一个个人端 `Dispatcher`（`tool_slots` 全局共享），不因分支数增加工具槽。
- 父等待时不占任何名额，内存/在途计量随实际请求保留。

## 取消与重启恢复

- 取消根任务：`cancel_generation += 1`，父 runner 取消后 `_run_with_branches` 的 `finally` 级联取消所有分支 runner；分支 attempt 按 `root_task_id` 一并结算。
- 续作：新一代开始时，父汇总尚未开始（父检查点为空）则把所有非 SUCCEEDED 分支重置为 READY/PENDING 重跑，已 SUCCEEDED 分支跳过；分支保留各自 `checkpoint`，UNKNOWN attempt 以原身份经 executor/gateway 核对确认（不重复执行、不重复结算），干净失败的 attempt 建立新 Attempt 重试。父汇总一旦写入检查点，汇合结果即固定，续作只继续父汇总。

## 验证

`tests/integration/test_branches.py`：

1. 两分支并行 + 父汇总：工具真实并行（并发峰值 ≥ 2）、并行墙钟 < 顺序和、父等待期间 `WAIT_DEPENDENCY`、模型请求 `root_task_id` 均为根任务。
2. 依赖汇合：`branch-b depends_on branch-a` 时 b 的工具严格晚于 a 完成。
3. 必需分支失败 → 根任务 FAILED（`BRANCH_FAILED`）；快速失败并取消在途兄弟分支。
4. 可选分支干净失败 → 根任务 SUCCEEDED；可选分支结果未知 → 分支 UNKNOWN/RECONCILING，根任务 UNKNOWN，不汇总。
5. 父取消 → 所有分支 CANCELLED。
6. 续作：UNKNOWN 分支工具以原身份核对不重放；失败的依赖分支重跑后后继分支运行；父汇总续作不重复注入 `branch_results`。
7. 父汇总首个请求保留 system 提示与原 instruction；跨任务同名分支记录互不覆盖。
8. 契约：环形依赖与 chat 模式分支被拒绝；绕过校验的环以 `BRANCH_DEADLOCK` 结束。
9. 资源上限：`tool_slots=1` 下分支工具并发峰值 ≤ 1；消耗 ≤ 根预算。

## API 观察

`TaskState.branches` 随任务详情接口返回：每个分支的 `state`/`outcome`/`result`/`error` 均可观察，父任务 `WAIT_DEPENDENCY` 表明正在等待分支汇合。
