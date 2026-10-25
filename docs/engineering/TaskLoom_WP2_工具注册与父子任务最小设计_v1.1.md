# TaskLoom WP2 工具注册与父子任务最小设计

> 个人端 Agent/Harness、多阶段工具执行与 OpenClaw 接入

**版本** v1.1 · **日期** 2026-10-25 · **状态** STEP 1 已实现，STEP 2 及之后为设计 · **读者** 项目成员与指导教师

## 1. 范围与状态

WP2 的目标是把个人端运行时从单一文档统计流程扩展为可审计的多步骤、多工具和受控子 Agent 执行面，并为资源调度实验提供真实工作负载；OpenClaw 接入属于独立的后续对照工作。

本文区分三类内容：

1. **已实现并可由代码与测试核验的约束**，见第 2—4 节；
2. **为后续实现设定但尚未落地的父子任务设计**，见第 5 节；
3. **尚未接入的 OpenClaw 对照路径**，见第 6 节。

当前结论只覆盖仓库中可直接检查的实现。平台专用执行测试依赖 Linux；Windows 编辑环境不能替代 Ubuntu 虚拟机或 WSL2 上的真实子进程、cgroup 与 `fcntl` 验证。

## 2. STEP 1 前的基线

在本次整改前，运行时存在以下边界问题：

- `run_agent` 的工具面以 `file_stats` 为中心，执行器遇到未知工具时会回退到文件统计，违反“未知输入必须失败关闭”的原则；
- 工具步骤只使用 `call_id` 标识，不同模型轮次重复使用同一 `call_id` 时会错误复用旧 Attempt 或旧结果；
- 工具参数主要依赖注册钩子检查，未统一拒绝未声明字段、缺失字段和错误 JSON 类型；
- 工具注册容量与 `ModelRequest.tools` 的上限不一致；
- 最终结果可从 `grep` 的命中词数或其他工具结果取得 `files/words`，无法保证最终报告对应全文统计口径。

上述描述是整改前的状态，不代表当前代码。

## 3. 当前已实现：有界工具与可审计执行

### 3.1 注册容量与模型工具面一致

`MAX_MODEL_TOOLS = 8` 同时约束两处：

- `ModelRequest.tools` 的长度不得超过 8；
- `ToolRegistry` 的默认容量为 8，显式配置超过 8 时在构造阶段以 `TOOL_LIMIT` 拒绝。

注册表继续拒绝同名工具（`DUPLICATE_TOOL`）和超过容量的注册请求（`TOOL_LIMIT`）。注册表是模型输出与主机执行之间的准入边界，但不替代执行器自身的失败关闭检查。

### 3.2 参数结构校验

`ToolSpec.parameters` 使用 JSON Schema 属性形式。当前实现固定采用以下最小语义：

- 所有已声明参数均为必需，未声明字段一律拒绝；
- 支持 `string`、`integer`、`number`、`boolean`、`object`、`array`、`null` 七种顶层类型；
- Python `bool` 不视为 `integer`；
- 结构校验先于工具特有的语义校验钩子 `validate`，schema 同时声明 `required` 和 `additionalProperties: false`。

结构或业务校验失败统一使用 `INVALID_TOOL`。当前限制是有意为之：在引入外部 JSON Schema 依赖或可空参数约定前，不宣称支持完整 JSON Schema。

### 3.3 工具执行显式分派

Agent 注册目录、执行契约、资源画像和工作进程统一支持四种受控工具：

- `file_stats` 返回全文文件数与按空白分词的全文词数；
- `grep` 返回字面量搜索的文件数、命中行词数、命中数和有限行样本；
- `list_files` 返回文件尺寸与校验值；`top_words` 返回有界的高频词列表。

其他工具名在契约准入时拒绝，不产生工具进程或结果文件。

客户端宿主在创建工具 Attempt 前也再次调用 `ToolRegistry.require()` 与 `ToolSpec.check()`。因此正常调用中的未知工具稳定返回 `INVALID_TOOL`，不会进入执行器；执行器测试则单独证明即使绕过宿主，未知工具仍会失败关闭。这构成双层保护，而不是依赖单一入口。

### 3.4 轮次化工具步骤身份

工具步骤身份定义为：

```text
{task_id}-model-{model_round}-tool-{call_id}
```

同一 `call_id` 出现在不同模型轮次时，对应不同 Step 与 Attempt。Attempt 初建时保存逻辑调用描述，并在派发前补齐完整 ToolRun 与带版本的 ToolClaim。名称和参数用于判断逻辑调用是否相同，CPU、内存等执行参数沿用原请求。逻辑调用描述示例：

```json
{"tool_name": "...", "arguments": {}}
```

复用规则如下：

- 同一步骤身份、同一工具、同一参数：复用已有成功结果；
- 同一步骤身份，但工具名或参数不同：返回 `ATTEMPT_CONFLICT`；
- 历史 Attempt 缺少请求摘要：不得猜测等价性，按冲突处理并失败关闭；
- 冲突检查发生在资源准入和执行器调用之前，不覆盖旧结果，也不启动新的执行。

旧检查点通过已保存模型回复验证名称和参数，建立轮次化键到原 Step 的别名。已有完整请求保留原样，未知执行仅按原 Attempt 查询。模型步骤的前驱通过别名引用已完成工具步骤；工具步骤的前驱引用产生该调用的模型轮次。

### 3.5 最终统计口径

最终结果的 `files` 与 `words` 只接受最近一次成功 `file_stats` 的结果作为权威来源。规则为：

- 只有 `grep` 或其他工具结果时，返回 `UNVERIFIED_RESULT`；
- 使用 `grep.words` 取代全文词数时，返回 `RESULT_MISMATCH`；
- 多次 `file_stats` 时，以完成顺序中的最近一次结果为准；
- 历史检查点中没有工具调用元数据的旧结果，不能成为新的权威来源。

`grep.words` 的语义是命中行的词数，不是全文词数；这一点在数据结构、测试与报告口径中必须保持区分。

## 4. 关键不变量与稳定错误码

| 不变量 | 实现位置 | 违规结果 |
|---|---|---|
| 工具面不超过模型请求上限 | `contracts/models.py`、`runtime/registry.py` | `TOOL_LIMIT` |
| 工具名必须已注册 | Agent 与客户端宿主双重校验 | `INVALID_TOOL` |
| 参数不含额外字段且类型正确 | `ToolSpec.check()` | `INVALID_TOOL` |
| 工具步骤按模型轮次隔离 | `tool_step_id()` 与工具 Attempt | 独立 Attempt |
| 同身份不同请求不得复用 | `ClientService._attempt()` | `ATTEMPT_CONFLICT` |
| 未知执行器工具不得回退 | `execution/tool.py` | 失败且无结果产物 |
| 最终全文统计只能来自 `file_stats` | `runtime/agent.py` | `UNVERIFIED_RESULT` / `RESULT_MISMATCH` |

## 5. 尚未实现：受控子 Agent 与依赖等待

以下内容是 STEP 2 的设计依据，当前运行时尚未提供父子任务创建、并行分支或 `WAIT_DEPENDENCY` 状态转换。

### 5.1 父子关系与归属

`Agent(agent_id, root_task_id, parent_agent_id=None)` 已具备字段，但 `ClientService.create()` 当前只创建根任务 Agent。后续设计为：

1. 子任务登记时生成带 `parent_agent_id` 的 Agent 记录；
2. 子 Agent 的 Step、Attempt 与产物继续以根任务作为记账和聚合单位；
3. 父子关系可查询，但界面仍按根任务聚合。

### 5.2 预算与分支上限

子 Agent 不新增根任务预算或服务份额。所有分支共享根任务预算，并对每个 Agent 的派生数量、根任务总分支数和步骤数设置硬上限，避免扇出导致预算失真或调度饥饿。

### 5.3 依赖等待

父任务等待子结果时应释放模型或工具许可，并记录 `WAIT_DEPENDENCY`。子结果到达后产生唤醒事件，调度器重新检查依赖与资源条件，再原子取得许可继续执行。该状态不得与 `WAIT_RESOURCE`、`WAIT_QUOTA` 混用。

### 5.4 向 WP3 输出的就绪步骤契约

子 Agent 产生的模型步骤进入个人端就绪队列时，应携带 `root_task_id`、`agent_id`、`parent_agent_id`、继承的优先级与期限，以及可核算的成本信息。该契约目前是设计输出，不是已实现接口。

## 6. 尚未实现：OpenClaw 接入

OpenClaw 作为独立对照对象，后续按[开发路线与 OpenClaw 对照方案](../TaskLoom_开发路线与OpenClaw对照方案.md)固定版本，先完成最小模型请求接入，再核验 Session/Run 与 Task/Step/Attempt 的映射、工具调用和取消语义。当前代码没有 OpenClaw 接入，也不以现有固定模型夹具代替其运行证据。

## 7. 协作接口

| 协作方 | 已提供或待设计内容 | 状态 |
|---|---|---|
| WP1 | 任务、Step、Attempt 与工具结果契约 | STEP 1 已实现 |
| WP3 | 带根任务归属、优先级、期限和成本的就绪步骤 | 待设计 |
| WP4 | 工具名、输入版本、取消代次与资源需求 | `file_stats`、`grep` 已实现 |
| WP6 | 依赖、等待原因与状态事件 | 待实现 |

涉及 `client/service.py` 的调度接口变更应与 WP3 使用小范围变更协作，避免同时修改公共契约、调度策略和执行适配器。

## 8. 阶段与验收边界

| 阶段 | 内容 | 状态与验收条件 |
|---|---|---|
| STEP 1 | 有界注册表、`file_stats + grep`、多轮调用、轮次化身份、参数校验、统计口径 | 已实现；单元和客户端集成测试通过，Linux 真实执行器测试待复核 |
| STEP 2 | 受控子 Agent、共享预算、依赖等待 | 未实现；需父子查询、分支不增预算和 `WAIT_DEPENDENCY` 定向测试 |
| STEP 3 | 调度实验工作负载 | 未实现；应在 STEP 2 完成后固定基线与测量口径 |
| STEP 4 | OpenClaw 最小接入与对照 | 未实现；需固定版本、脱敏运行记录与覆盖范围说明 |

## 9. 验证边界

- Windows 可执行注册表、Agent、客户端宿主和不依赖 Linux 适配器的集成测试；
- 真实工具子进程、cgroup 和文件系统清理需在 Ubuntu Linux 虚拟机或 WSL2 中运行；
- Windows 缺少 `fcntl` 时的导入失败属于平台边界，不得通过修改 Linux 适配器规避；
- 只有在 Linux 环境实际运行并保留输出后，才能宣称真实执行器、Linux 专项或完整验收通过。
