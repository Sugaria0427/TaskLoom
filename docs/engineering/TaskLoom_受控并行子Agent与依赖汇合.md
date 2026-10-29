# 受控并行子 Agent 与依赖汇合

工作包：`[L2] 实现受控并行子 Agent、依赖汇合与取消恢复`（Issue IKR9I1）。
接入位置：`runtime/agent.py`、`runtime/ports.py`、`client/service.py`、`domain/model.py` 与公共契约。

## 目标

让一个根任务拆成受控的并行分支（例如同时解析资料、核对来源），必要结果齐备后由父 Agent 汇合为最终成果。普通单 Agent 对话行为保持完全兼容。

## 契约

- `TaskCreate.branches: list[BranchSpec]`（最多 8 个，有界分支数）。
- `BranchSpec`：`agent_id`（确定性分支身份）、`instruction`（分支指令）、`required`（必需/可选）、`depends_on`（依赖的分支 agent_id，全部 SUCCEEDED 后才启动）。
- `TaskState.branches: dict[str, BranchState]`：分支的持久化运行时状态（独立 `checkpoint`、`state`/`outcome`、`result`、`error`、`consumed_units`/`reserved_units`），随任务记录一并落盘，重启可恢复。
- 分支 Agent 身份复用 `domain/model.py::Agent(agent_id, root_task_id, parent_agent_id)`；创建分支时落一条带 `parent_agent_id` 的 agent 记录。

## 接入路径

| 层 | 改动 |
|---|---|
| `contracts/models.py` | 新增 `BranchSpec`、`BranchState`；`TaskCreate.branches`（含唯一性/依赖存在性/自依赖校验）；`TaskState.branches`。 |
| `runtime/agent.py` | `run_agent` 增加 `agent_id` 与 `instruction` 可选参数：步骤身份与初始化提示按分支隔离，默认值保持旧行为。 |
| `client/service.py` | `_run` 检测 `specification.branches` 后进入 `_run_with_branches`：幂等初始化分支状态 → 按依赖拓扑启动 `_run_branch` 并行 runner → 等待汇合 → 分支结果注入父 checkpoint → 父 `run_agent` 做最终汇总。新增 `BranchHost`（分支的 model/tool/checkpoint 适配，全部携带根任务身份）。`_attempt`/`model`/`tool` 支持 `agent_id`，分支步骤身份与依赖检查在分支自己的 checkpoint 空间内完成。 |

## 执行模型

- 父任务在分支运行期间置 `WAIT_DEPENDENCY`，**不发起任何模型/工具请求**——把执行许可安全让出给分支；汇合后回到 `RUNNING` 由父 Agent 汇总。
- 每个分支运行自己的有界模型—工具循环（独立 `Checkpoint`、独立步骤身份、独立 `max_steps`）。
- 依赖汇合：`depends_on` 中所有分支 SUCCEEDED 才启动后继分支；依赖已终态但失败时，必需分支直接判死（`BRANCH_DEPENDENCY_FAILED`），可选分支跳过并标记 FAILED。
- 失败策略：任一必需分支 FAILED/CANCELLED → 根任务 FAILED（`BRANCH_FAILED`，记录分支错误），其余在途分支级联取消；仅可选分支失败 → 父用成功分支结果继续。

## 预算与资源（分支不额外获得份额）

- 所有分支模型请求 `ModelRequest.root_task_id = 根任务`：L3 本地调度与 L4 平台准入都按根任务计费与限流，分支数量不扩大根预算或公平份额。
- 分支工具请求走同一个个人端 `Dispatcher`（`tool_slots` 全局共享），不因分支数增加工具槽。
- 父等待时不占任何名额，内存/在途计量随实际请求保留。

## 取消与重启恢复

- 取消根任务：`cancel_generation += 1`，父 runner 取消后 `_run_with_branches` 的 `finally` 级联取消所有分支 runner；分支 attempt 按 `root_task_id` 一并结算。
- 重启：`resume` 幂等——已 SUCCEEDED 分支跳过，PENDING/FAILED/UNKNOWN 分支从各自 `BranchState.checkpoint` 续跑；UNKNOWN 的分支 attempt 保留原身份，经 executor/gateway 核对后确认（不重复执行、不重复结算）。

## 验证

`tests/integration/test_branches.py`（8 项，全部通过）：

1. 两分支并行 + 父汇总：两分支工具真实并行（并发峰值 ≥ 2）、并行墙钟 < 顺序和、父等待期间 `WAIT_DEPENDENCY`、所有模型请求 `root_task_id` 指向根任务、分支/父各自 SUCCEEDED。
2. 依赖汇合：`branch-b depends_on branch-a` 时 b 的工具严格晚于 a 完成。
3. 必需分支失败 → 根任务 FAILED（`BRANCH_FAILED`）。
4. 可选分支失败 → 根任务继续 SUCCEEDED。
5. 父取消 → 所有分支 CANCELLED。
6. 重启续作：executor 崩溃后任务 UNKNOWN/RECONCILING，resume 后原工具 attempt 身份保留（不重执行），根任务 SUCCEEDED。
7. 资源上限：`tool_slots=1` 下两分支工具并发峰值 ≤ 1；消耗 ≤ 根预算。

回归：`python3.12 tools/project.py check` 全部通过（ruff、mypy、契约 3 kept 0 broken）；`tools/project.py test` 全量通过。

## API 观察

`TaskState.branches` 随任务详情接口返回：每个分支的 `state`/`outcome`/`result`/`error` 均可观察，父任务 `WAIT_DEPENDENCY` 表明正在等待分支汇合。
