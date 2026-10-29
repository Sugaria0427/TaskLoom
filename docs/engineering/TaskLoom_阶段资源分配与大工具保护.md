# 阶段资源分配与大工具接纳保护

对应工作包：[IKR9I2](https://gitee.com/Sugaria/TaskLoom/issues/IKR9I2)。
本增量基于 `644aae0`；工具按 Attempt 分配，公平份额继续归根任务。并行子 Agent 在独立工作包开发，合并后可沿用相同资源协议。

## 1. 已接入的执行链

`ClientService.tool → Dispatcher → PhaseResourcePool → ExecutorHTTP → ProcessExecutor → 工具进程`。

- 旧工具一次申请完整画像，默认工具清单和模型预算封套保持原样。
- `staged_tools = true` 时增加 `staged_top_words(top_n)`。L2 根据注册画像保存初始 128 MiB 和最大 384 MiB 的声明；同一真实进程保留文件内容，先申请 256 MiB 才分词，再申请 384 MiB 才汇总词频。申请等待期间保留进程名额和已有内存许可，返回可核验的文件数、词数和词频。
- 有 cgroup v2 时，初始 `memory.max = 128 MiB`；每次授权先扩展对应 cgroup 上限，再发布工作进程的继续许可。没有 cgroup 时明确只有进程组保护，不能把资源声明当成物理内存硬限制。
- 各阶段数字是保守的资源许可，不代表进程实际使用了全部内存。实际峰值继续读取执行回执。

## 2. 单一占用记录与银行家

`policies/resources.py::PhaseResourcePool` 复用原资源池接口，持有唯一的实际分配和最大需求。`PhaseBanker` 只对该记录的快照试算，不另存一份可独立变更的运行账。

准入顺序：验证声明与容量 → 检查各维空闲 → 试分配 → 搜索安全序列 → 提交。安全序列不规定实际派发顺序；Dispatcher 继续采用三档 WRR 与档内根任务轮转。

银行家的证明维度为工具名额和内存。`artifact_bytes` 仍参加整体原子准入和原有留存预算检查，但不参加可归还资源的安全证明：进程停止后留存的文件不会因释放预留而自动消失。本增量没有实现文件系统磁盘配额，也没有声称覆盖任意 shell 指令的磁盘写入。

`declare()` 支持最大需求的原子更新；低于当前占用或破坏安全状态的更新保持旧值。首个受控工具采用固定、版本化的最大需求，不提供用户任意修改活动进程最大需求的 HTTP 接口。

## 3. 阶段协议与恢复

`ToolRun.resource_protocol` 默认为 `one-shot`，阶段工具使用 `phase-v1`，并保存 `maximum_memory_bytes`。旧持久记录缺少新字段时按默认值读取；旧工具的发送请求省略默认字段。

`ToolPhase` 包含 `attempt_id / generation / version / memory_bytes`。派生申请身份为 `attempt_id:phase:version`，该版最多两个扩展阶段。工具不能更换代次、跳过前序或任意增大画像。

1. 工具将申请原子发布到所属 Attempt 目录，等待相同版本的授权文件。
2. `query()` 将已校验申请放入 `ToolState.pending_phase`；个人端沿现有轮询链取得申请。
3. 资源所有者进行安全准入，将绝对目标占用与 `AttemptState.phase_version / phase_allocation` 保存到原事务及事件中，然后调用 `POST /phases/grant`。
4. 执行器校验拥有者、请求内容、取消代次及前序，应用内存限制，保存 `phase_grants` 回执后发布继续许可。
5. 重传同一授权返回原记录并重新发布最新许可，不再累加占用。迟到授权不能恢复已取消、已停止或执行结果未知的进程。

客户端启动先核对执行回执、重建未确认占用，再开放工具派发。恢复使用保存的已扩展占用和原最大需求。原执行结果未知时保留占用并查询原身份，不自动重新执行。根任务恢复产生新控制代次，不会更换正在核对的原执行身份。

取消意图保持先落盘；实际停止确认后才归还。等待到任务期限会移除排队与保护；未启动请求保存失败终态，停止仍未知的执行保留占用。超出总容量的声明直接保存 `RESOURCE_TOO_LARGE` 失败，队列已满使用 `LOCAL_QUEUE_FULL`。

## 4. 大工具接纳保护

正常情况继续允许可行工具旁路不可行工具。等待达到 5 秒或累计被竞争请求旁路 8 次后，最早等待的候选取得保护；阈值可配置。

- 单个资源域一次只保护一个请求，不为它提交零碎的实际分配。
- 首版采取保守排空：暂缓会竞争其任一资源维度的新请求，允许已有持有者通过安全准入继续完成。此规则可能让部分剩余容量暂时闲置，对照实验同时报告轻任务等待和吞吐代价。
- 保护目标仍使用原优先级参与 WRR；取消、失效或开始执行后解除保护。优先级更新不重置等待时间和旁路计数。
- 定时器可独立触发保护，不要求恰好有其他任务完成。预留之后、回调首次运行之前发生取消也会清理等待者和资源。
- 活性依赖需求不超过容量、已有持有者能够在有限时间内结束并归还资源。永久未知执行或外部永久阻塞不属于可承诺固定等待上限的场景。

保护计时和旁路计数为当前排队实例的调度状态，重建排队时重新计算；原执行占用和阶段授权则持久保存。

## 5. 配置与观察

个人端 TOML 示例（现有平台账号配置仍沿用原方式）：

```toml
tool_slots = 2
memory_budget_mib = 512
staged_tools = true
phase_banker = true
large_tool_protection = true
protection_seconds = 5.0
protection_bypasses = 8
```

通过既有客户端配置入口加载，例如 `python tools/project.py client --config configs/dev/alice.toml`。客户端先识别执行器能力；旧执行器缺少阶段协议时，明确拒绝提交阶段工具。生产默认不公开额外阶段工具，避免增加旧请求的工具描述和 Token 预算。

`GET /api/v1/resources` 保留旧字段并新增：

- `banker`：证明维度、最大需求、安全序列、阻塞原因、最近 128 条准入记录。
- `tool_protection`：保护目标、等待秒数、旁路次数、原优先级、保护与阻塞原因。
- Attempt 和事件保存阶段版本与已授权占用。安全序列按稳定身份顺序搜索，便于复核。

`phase_banker = false` 与 `large_tool_protection = false` 是独立算法对照开关，不改变模型平台调用路径或费用账本。关闭银行家后阶段负载可能进入不安全等待，仍受工具超时及真实停止规则约束。

## 6. First Fit 与 SCAN 课程扩展

- `policies/first_fit.py`：在真实应用自有 `bytearray` 中执行首次适应查找、分割、读写、释放和相邻合并。缓冲区最多 16 MiB、活动块最多 4096 个；报告总空闲、最大连续空闲、外部碎片、元数据项数及查找次数。不是替换 Python 或内核分配器。
- `policies/disk.py`：显式磁道与到达时间，单位寻道时间、单位服务时间，最多 512 个请求。SCAN 到物理边界后折返，并接纳扫描途中的新请求；FIFO 使用相同输入轨迹。报告服务顺序、寻道距离和等待，不代表 SSD 或真实文件读取优化。

统一复现入口：

```bash
python tools/project.py resources-demo --repeats 3 --output agent-workspace/outputs/resources-demo.json
```

该命令同时给出银行家受控轨迹、生产 Dispatcher 的成对固定到达负载、First Fit 缓冲区结果与 FIFO/SCAN 同轨迹结果。保护对照分别报告大工具等待、轻工具 P95、吞吐、总耗时和整个测试进程 CPU 开销，保留负面变化；这些回调负载不冒充真实工具进程或付费模型实测。

## 7. 验证与边界

```bash
python tools/project.py check
python tools/project.py test unit integration l4
python tools/project.py test linux
python tools/project.py verify-isolated
```

新增关键验证：

- `test_phase_resources`：安全拒绝、动态最大需求回滚、绝对占用重传、未知占用恢复及多资源原子性。
- `test_resource_protection`：持续补充轻工具时大工具推进、独立计时唤醒、取消解除保护、已有持有者继续完成及回调首次运行前取消。
- `integration/test_tool_phases`：两个真实工具进程经 HTTP 契约扩展、结果核验、授权响应丢失后沿原 PID 恢复、根控制代次变化、重复授权及取消、等待过期和不可能声明的持久失败。
- `linux/test_tool_phases`：真实 cgroup 上限随授权从 128 扩展到 256、384 MiB，采集内存峰值并确认最终组回收。
- `test_os_models`：缓冲区内容隔离、外部碎片与相邻合并，SCAN 边界折返、动态到达和轨迹校验。
- `test_resource_bench`：相同工作负载、完成数、容量约束和最终占用归零。

正式结果、环境、验证提交及跳过项随 PR 记录。本工作包没有改变 L3/L4 模型发送窗口，没有接入共享读写锁或 single-flight，也不把安全序列扩展为任意文件锁和外部依赖的系统级无死锁证明。相关工作分别由 IKR9K4、IKR9K3 承担。
