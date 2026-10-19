# TaskLoom WP2 工具注册与父子任务最小设计

> 个人端 Agent/Harness、多步骤与 OpenClaw 接入 · 首批设计

**日期** 2026-10-19 · **状态** 首批开发建议 · **读者** 项目成员与指导教师

## 1. 背景与目标

WP2 工作包目标：将当前文档统计循环扩展为能够推进多步骤、多工具和受控子 Agent 的个人端运行时，为资源调度提供真实、可追踪的任务；并建立固定版本 OpenClaw 的接入与对照路径（见 [Gitee Issue IKM3JQ](https://gitee.com/Sugaria/TaskLoom/issues/IKM3JQ)）。

当前 0.2 运行时的关键约束：

- `runtime/agent.py` 的 `run_agent` 硬编码单一工具 `file_stats`，其余工具直接抛 `INVALID_TOOL`；模型请求的工具面来自常量 `FILE_STATS_TOOL`。
- 每条用户消息建立一个独立根任务；`domain/model.py` 中 `Agent.parent_agent_id`、`Step.predecessors`、`WorkState.WAIT_DEPENDENCY` 已定义但运行时不使用。
- 等待只区分 `WAIT_RESOURCE`（工具资源）与 `WAIT_QUOTA`（模型额度）两类，未区分依赖等待与个人请求名额等待。

本设计先交付第一阶段（Phase 1）：**有边界的工具注册表**，将硬编码单工具循环改为注册表驱动的多工具路由，为多步骤工作流和受控子 Agent 提供准入边界；同时给出父子任务的最小设计，作为 Phase 2 的实施依据。

## 2. 工具注册表设计（Phase 1，本 PR 实现）

### 2.1 数据结构

新增 `runtime/registry.py`，提供两个核心对象：

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: JsonObject                      # JSON Schema 属性形式，空表示无参数
    demand: Resources                           # 工具资源需求（槽位/内存/产物）
    validate: Callable[[ToolCall], None] | None  # 派发前的业务校验钩子
```

```python
class ToolRegistry:
    capacity: int                               # 注册上限，默认 16
    def register(spec: ToolSpec) -> ToolSpec    # 唯一性 + 容量准入
    def get(name: str) -> ToolSpec | None
    def require(name: str) -> ToolSpec          # 未注册抛 INVALID_TOOL
    def schemas() -> list[JsonObject]           # 模型工具面，注册序
    def names() -> tuple[str, ...]
```

### 2.2 准入规则

- **注册准入**：同名工具拒绝注册（`DUPLICATE_TOOL`）；超过容量上限拒绝（`TOOL_LIMIT`）。模型只能请求已注册工具，注册表即模型输出与主机执行之间的边界。
- **调用校验**（`ToolSpec.check`）：未注册工具抛 `INVALID_TOOL`（保持原错误码与语义）；无参数定义的工具收到参数时拒绝；有参数定义的工具将具体参数交给 `validate` 钩子做业务校验（如必填项、枚举值），避免引入外部 JSON Schema 依赖。
- **资源需求**：`demand` 沿用个人端工具域的资源口径（槽位、内存、产物字节），与 `client/service.py` 的 `Dispatcher` 准入保持一致；WP4 扩展真实工具进程时按同一字段提供成本。

### 2.3 与现有实现的兼容

- `default_registry()` 注册与旧 `FILE_STATS_TOOL` **字节级一致**的 `file_stats` schema，`ModelRequest.tools` 内容不变，已有请求与验收不感知变化。
- `run_agent` 增加 `registry: ToolRegistry` 参数；`client/service.py` 在 `ClientService.__init__` 持有 `self.registry = default_registry()`，`model()` 的 `tools=self.registry.schemas()`。
- `ToolRun.tool_name` 的 `Literal["file_stats"]` 保持不变；executor 层工具执行在本阶段不扩展，由 Phase 2 统一处理。

### 2.4 本阶段改动文件

| 文件 | 改动 |
|---|---|
| `src/taskloom/runtime/registry.py` | 新增：`ToolSpec`、`ToolRegistry`、`default_registry` |
| `src/taskloom/runtime/agent.py` | `run_agent` 注册表驱动；删除 `FILE_STATS_TOOL` 常量 |
| `src/taskloom/client/service.py` | 注入默认注册表；`tools` 改由 `registry.schemas()` 提供 |
| `tests/unit/test_registry.py` | 新增：注册/查重/容量/参数校验/schema 输出 |
| `tests/unit/test_agent.py` | 新增：注册表驱动闭环、未注册工具拒绝、参数工具路由、步数上限 |

## 3. 父子任务最小设计（Phase 2）

### 3.1 现状

`Agent(agent_id, root_task_id, parent_agent_id=None)` 已具备父子字段；`ClientService.create()` 创建根任务时只建一个 Agent，`budget_units` 在根任务独立核算，未共享。

### 3.2 设计要点

1. **建立父子关系**：`create()` 支持可选 `parent_task_id` / `parent_agent_id`；子任务登记时生成 `Agent(agent_id, root_task_id=根任务, parent_agent_id=父 Agent)`，写入 `agent` 记录供查询。
2. **根任务归属**：所有子 Agent 的步骤、尝试、产物继续以根任务为记账单位（`AttemptState.root_task_id` 不变），界面按根任务聚合。
3. **共享预算与服务份额**：子 Agent 不新增根任务预算；`_save_attempt` 的 `PRIVATE_BUDGET` 检查仍以根任务 `budget_units` 为准，分支数增加不放大份额（与现有 `test_root_fanout_does_not_create_extra_round_robin_turns` 的公平语义一致）。
4. **分支与步骤上限**：`Agent` 增加分支数上限（如每个 Agent 至多派生 N 个子 Agent），子 Agent 自身受根任务 `max_steps` 约束，防止失控扇出。
5. **父等待子结果**：父任务等待子 Agent 结果时，将其持有的模型/工具运行许可让出（`Dispatcher` 释放），等待原因记为 `WAIT_DEPENDENCY`；子结果到达产生唤醒事件，调度器复查条件后原子取得许可继续。

### 3.3 就绪步骤契约（向 WP3 输出）

子 Agent 产生的模型步骤进入个人端就绪队列时，携带：

- `root_task_id`（根任务归属）
- `agent_id` / `parent_agent_id`（分支归属）
- `priority`（继承根任务优先级）
- `deadline_at`（根任务期限）
- `cost`（`reserve_units`，成本信息）

## 4. 多工具推进与 OpenClaw 接入（Phase 3—4）

- **多工具推进**：在注册表基础上扩展 executor 工具执行（`ToolRun.tool_name` 去 Literal 限制），引入真实第二工具（如 `grep`、`wc`），使 `run_agent` 出现"模型→工具A→模型→工具B→模型"的多步链；`Step.predecessors` 据此登记真实依赖。
- **OpenClaw 接入**：按[开发路线与 OpenClaw 对照方案](../TaskLoom_开发路线与OpenClaw对照方案.md)固定版本（稳定版 `59cb44796b0f1f0daa0e37af8b1495abb45d9bc1` 与 main 快照 `6a64431a10b638958cdeccc782f2b36074274776`），先交付最小模型请求接入样例（自定义 Provider + `baseUrl`），再核验 Session/Run → Task/Step/Attempt 映射与工具调用、取消的接入位置。

## 5. 协作接口

| 协作方 | 本工作包交付 |
|---|---|
| WP1 | 公共任务及事件契约（含本设计新增的工具注册事件） |
| WP3 | 带根任务归属、优先级、期限、成本的就绪模型步骤 |
| WP4 | 工具名称、输入版本、取消代次与阶段需求 |
| WP6 | 实际依赖、等待原因及状态事件 |

涉及 `client/service.py` 的调度接入点与 WP3 使用小 PR 协作。

## 6. 分阶段交付与验收

| 阶段 | 内容 | 验收依据 |
|---|---|---|
| Phase 1（本 PR） | 工具注册表 + 注册表驱动 Agent 循环 | 单元测试通过；`file_stats` 行为不变；模型工具面字节级兼容 |
| Phase 2 | 受控子 Agent 与依赖等待 | 两个根任务 + 子 Agent 并行，父子可查，分支不增预算；`WAIT_DEPENDENCY` 定向测试 |
| Phase 3 | 多工具真实推进 | 多模型请求 + 两类真实工具的工作流产出可校验成果，依赖顺序正确 |
| Phase 4 | OpenClaw 接入 | 固定版本最小接入配置、脱敏运行记录、覆盖范围说明 |
