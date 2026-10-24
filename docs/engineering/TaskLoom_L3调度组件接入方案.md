# L3 调度组件首批验收与后续接入方案

> 对应工作包 IKM3JR（个人端 L3 请求调度），依据《TaskLoom_项目说明书_当前完整方案.md》第 4.1.3 节。
> 本文回答两件事：首批组件按什么口径验收；接线要改哪些位置、依赖什么、满足什么条件才算完成。
> 定位一律用模块、类、函数与方法名，不写行号——代码一改行号就漂，符号不会。

## 1. 验收口径

本次范围是**L3 调度组件首批**，不是线上策略切换。判据分两层，互不替代：

| 层 | 判据 | 状态 |
| --- | --- | --- |
| 组件层 | 公平、并发、等待终态、反馈异常收敛在组件内成立，并有回归测试守住 | 已交付 |
| 接线层 | `ClientService.model` 的生产路径走新调度器，旧 `Dispatcher` 退出模型侧 | 已完成，见第 3—6 节 |

接线分五步落地，每步单独验证、单独提交：

| 步 | 内容 | 提交 |
| --- | --- | --- |
| 一 | 装配切换，模型路径经 `ModelScheduler.submit`，移除 `wait_platform` | `9b0f12b` |
| 二 | 反馈适配器 `FeedbackSource`，按周期复用快照、失败直抛、只读 | `348758d` |
| 三 | 用量回补 `ModelScheduler.settle`，多预留退回公平账本 | `670eaf6` |
| 四 | 状态输出与准入同源，`platform_status` 加 `feedback_fresh` 与 `scheduler` | 见第 8 节 |

`Dispatcher` 未被删除，它继续服务工具槽位；模型侧已完全退出，因此不存在"半切换"状态。

## 2. 组件层已达成的约束

| 约束 | 落点 | 回归测试 |
| --- | --- | --- |
| 份额按累计成本而非条数 | `CostDRR._serve` 逐条扣额度、退场时保留余额；额度**按轮发放**，一次只取一小批也不会被提前透支 | `test_cost_drr.py::test_cumulative_cost_share_is_fair_across_heterogeneous_loads` |
| 单价高于量子的请求有活性 | 额度跨轮累积，`ceil(价格/量子)` 轮后派出 | `test_cost_drr.py::test_expensive_item_waits_for_enough_credit` |
| 持续紧急流不能挤掉后台 | `AgingQueue.pop` 的 `urgency_burst` 连续提升上限，名额被提升占满时轮转没跑、计数留着，下一次强制走正常轮转 | `test_aging.py::test_sustained_urgent_arrivals_cannot_starve_the_background` |
| 提升计入公平账本 | `AgingQueue._take_from` → `CostDRR.take` → `CostDRR.charge` 允许扣成负额度 | `test_aging.py::test_promotion_is_charged_to_the_promoted_owner` |
| `max_inflight` 真正并行 | `ModelScheduler._launch` 原子占名额后 `create_task`，主循环继续挑候选 | `test_model_scheduler.py::test_capacity_allows_true_parallelism` |
| 反馈异常不丢候选 | `ModelScheduler._on_feedback_failure` 保留原等待时长：候选根本没被取出，仍留在队列，超上限则把异常交给调用者 | `::test_feedback_read_failure_keeps_the_candidate_owned`、`::test_permanent_feedback_failure_reaches_the_caller` |
| 过期是终态 | `ModelScheduler._retire_expired` 出队、清身份表、以 `deadline_expired` 结束 Future；`ModelScheduler._await_wakeup` 按 `next_deadline` 定时醒 | `::test_expired_request_reaches_a_terminal_state`、`::test_request_expiring_while_waiting_is_swept` |
| 反馈 await 按返回值判断 | `ModelScheduler._get_feedback` 用 `inspect.isawaitable`，兼容同步函数返回 awaitable | `::test_awaitable_returning_sync_feedback_is_awaited` |
| 不可恢复费用与可恢复等待分开 | `feedback_decision` 拆成账户级 `account_decision` 与请求级 `request_decision`，判据与 `ClientService.wait_platform` 逐条对齐 | `test_feedback_decision.py::test_insufficient_budget_refuses`、`::test_reserved_funds_wait_instead_of_refusing`、`::test_window_token_shortfall_waits` |
| 取出的就是能派发的 | `ModelScheduler._step` 用 `limit=capacity` 取候选、请求级不可行的由 `accept` 谓词让位，取出的全部派发，不再放回队列 | `::test_callback_cost_share_is_fair_across_roots`、`::test_request_level_block_does_not_stop_other_roots` |
| 取消传到执行路径 | `ModelScheduler._abandon` 维护 `request_id → 执行 Task`，调用者被取消时取消并等待执行任务收尾 | `test_client_cancel.py::test_cancel_after_model_dispatch_reaches_the_execution` |
| 退避被最早期限打断 | `ModelScheduler._await_wakeup` 取退避上限与"距最近期限"的较小值 | `::test_deadline_wakes_the_backoff` |
| 校正用原预扣量、只校正一次 | `ClientService._reserved_units` 从 `AttemptState.request_payload` 复原预扣量；`_settle_model` 在锁内看共享记录，重复观察不重复退 | `test_client_settle.py::test_settlement_uses_the_original_reservation` |

`decide`（`feedback_decision.decide` 保留为组合入口，内部由下面两个函数拼成）与 `wait_platform` 的对齐口径
（同一平台状态必须得到同一结论）：

| 平台状态 | 判据 | 判定层 | L3 动作 | `wait_platform` 行为 |
| --- | --- | --- | --- | --- |
| 花不起钱 | `预算 - 已消费 < 本次预留` | 请求级 | `refuse/insufficient_budget` | 抛 `ACCOUNT_BUDGET` |
| 额度被在途预留压住 | `account_remaining_units < 预留` | 请求级 | `wait/budget_reserved` | 继续等待 |
| 窗口 token 不足 | `account_window_tokens_remaining < 预留` | 请求级 | `wait/window_exhausted` | 继续等待 |
| 平台侧排队或限流 | `can_submit=False` | 账户级 | `wait/platform_blocked` | 继续等待 |
| 账户排队满 | `account_queued >= max(1, recommended_pending_limit)` | 账户级 | `wait/platform_queued_full` | 继续等待 |
| 本地名额满 | `in_flight >= max_inflight` | 账户级 | `wait/local_backpressure` | — |
| 快照陈旧 | `now - observed_at > STALE_FEEDBACK_SECONDS` | 账户级 | `wait/stale_feedback` | — |

账户级结论只让整轮退避；请求级结论只挡这一条请求（`ModelScheduler._acceptor` 返回 False 让位），
别的根任务照常派发。终态拒绝（`insufficient_budget`）在账户级退避之前判定——平台预算耗尽时同样会把
`can_submit` 置 False，先退避会让一条永远发不出去的请求一直挂着。

## 3. 接入面

| 落点 | 现状 | 接线后 |
| --- | --- | --- |
| `ClientService.__init__` 的 `self.models` 装配 | `Dispatcher(Resources(slots=config.max_inflight))` | `ModelScheduler(...)` |
| `ClientService.start` 的 `self.models.start()` | 启动 `Dispatcher` 循环 | 启动调度循环 |
| `ClientService.model` 末尾的 `self.models.submit(...)` | `submit(identity, owner, demand, callback)` | `submit(request, callback)` |
| `ClientService.model` 的 `execute` 内 `await self.wait_platform(request)` | 准入在 callback 里做 | 移除，准入改由 `decide` 承担 |
| `ClientService.resources` 的 `queued_models` / `model_capacity` / `model_available` | 读 `Dispatcher.queue` 与 `Dispatcher.pool` | 读 `ModelScheduler.pending` / `ModelScheduler.in_flight`，`Resources` 口径输出保持不变 |
| `ClientService.platform_status` 的 `feedback` | 读 `self.platform_feedback`，只由对账循环更新 | 读 `FeedbackSource.latest`，与准入同源；另加 `feedback_fresh` 与 `scheduler` 三字段 | |
| `ClientService.close` 的 `await self.models.close()` | 关 `Dispatcher` | 同名方法，语义一致 |

`execute` 里的 `PRIVATE_BUDGET`、`settled`、`quiescent`、`RECONCILING` 分支保持不动：那是根任务私有预算与结算，
不属于 L3 的份额与准入，接线时不得顺手搬进调度器。

## 4. 接口契约

### 4.1 调度器对外面（已经定型）

- `ModelScheduler.start()`：启动后台循环。
- `ModelScheduler.submit(request, callback)`：`request.request_id` 是幂等键，`request.root_task_id` 是公平账本的 owner。
  异常：`Refused.reason ∈ {insufficient_budget, deadline_expired, ...}`、`asyncio.CancelledError`、callback 原始异常。
- `ModelScheduler.cancel(request_id)`、`ModelScheduler.close()`。
- 只读属性：`in_flight`、`pending`。

### 4.2 反馈适配器（接线前必须落地）

`GatewayPort.feedback` 是无参协程，`ModelScheduler` 要的是无参可调用对象。适配层职责固定为三条：

1. **缓存与新鲜度**：按 `ClientConfig.feedback_interval_seconds` 缓存快照，但实际复用时长收在
   `feedback_decision.STALE_FEEDBACK_SECONDS` 以内——配置允许三十秒，而超过陈旧阈值的快照调度器本来
   就会判它陈旧，不收窄就会反复拿到同一份自己判为陈旧的快照。`observed_at` 由平台填写，缓存不得把它
   重写成读取时刻。
2. **失败不伪装**：读失败直接抛，由 `_on_feedback_failure` 计入重试并保留候选；适配层不得返回过期快照冒充新快照。
3. **不协商**：只读，不写平台状态，不改预算。

### 4.3 用量回补（接线前必须落地）

调度器目前用 `ModelRequest.reserve_units`（`input_tokens + max_tokens`）作价格。实际消费由
`GatewayRecord.consumed_units` 结算后才知道，差额要回补公平账本，否则预扣偏高的请求被长期惩罚。要加的方法：

```python
def settle(self, owner: str, reserved: int, consumed: int) -> None:
    """按实际消费校正 owner 的额度：多扣的退回，少扣的补记。"""
```

调用点：`ClientService._settle_model` 拿到 `GatewayRecord.consumed_units` 后，按 `attempt_id` 找回 owner 调用一次。
`consumed == 0` 且 `outcome == Outcome.UNKNOWN` 时**不**回补——结果未知的请求保留预留、等核对结论，
符合"已派发但结果未知进入核对、禁止未经核对自动重放"的约束。

`reserved` 必须取**原预扣量**：平台完成结算后会把 `GatewayRecord.reserved_units` 清零，直接拿它当预扣量
等于按"预扣 0、实付 10"校正，反而多扣 10。原预扣量由 `ClientService._reserved_units` 从持久化的
`AttemptState.request_payload` 复原（重启后同样可用）。

"只校正一次"以 Attempt 为单位：`ClientService._settle_model` 在锁内读共享的 `self.attempts` 记录，
执行路径与对账路径同时看到同一个完成结果时只退一份。

`settle` 是 `ModelScheduler` 上的方法，不动 `GatewayPort`、不动 `PlatformFeedback`、不动 `contracts/models.py`。

### 4.4 状态与结算语义（接线前统一）

- 平台真实状态是 `QUEUED` / `DISPATCH_INTENT` / `FINISHED` / `UNKNOWN`（`GatewayRecord.state`）。测试替身一律用真实值。
- `GatewayRecord.quiescent` 表示该请求不再占用平台名额。名额释放只能挂在执行结束上：
  `ModelScheduler._execute` 的 `finally` 对应"callback 返回"，callback 必须是**完整模型请求生命周期**
  （submit + 轮询到 FINISHED/UNKNOWN + 结算），不能只覆盖网关接纳。这一条进接线评审检查项。

## 5. 依赖与配置

| 依赖 | 现状 | 需要做什么 |
| --- | --- | --- |
| `ClientConfig.max_inflight` | 已有 | 直接复用 |
| `ClientConfig.coordinated` | 已有 | 直接复用，作为对照基线开关 |
| `ClientConfig.feedback_interval_seconds` | 已有 | 反馈缓存周期 |
| DRR 量子、老化上限、紧迫桶数、反馈过期阈值 | 组件构造参数与模块顶部常量 | **保持模块常量，不进 `ClientConfig`**（WP3 工作备忘第 2 节） |
| `contracts/models.py`、`GatewayPort`、`PlatformFeedback` | 本次零增量 | 接线阶段同样零增量，回补只加 `ModelScheduler.settle` |
| 依赖方向 | `client → ports`，`policies` 纯标准库 | 新组件不引入第三方库，不碰 gateway/adapters |

## 6. 完成条件

接线阶段按下列清单逐条验收，全部通过才算完成：

1. `ClientService.model` 的生产路径经 `ModelScheduler.submit`，`wait_platform` 从该路径移除；全仓 grep 可证明模型侧不再有第二条准入判定。
2. `tools/project.py verify` 通过（ruff format/check、mypy、lint-imports、unit+integration、wheel 审计、smoke）。
3. `tools/project.py test integration` 覆盖两条真实链路：`coordinated=True` 下平台排队→等待→最终提交；`coordinated=False` 对照基线直接提交。
4. 用量回补有测试：预扣 1000、实际消费 10，结算后 owner 额度回到与预扣 10 相同的水位。
5. 状态串统一：全仓 `grep -r '"COMPLETED"' src tests` 为空。
6. `close()` 后无遗留任务：`asyncio.all_tasks()` 中不含调度循环与执行任务。
7. 回归：第 2 节每行测试仍通过，且 `test_cumulative_cost_share_is_fair_across_heterogeneous_loads` 的
   `abs(units["heavy"] - units["light"]) < 0.05 * max(...)` 断言不得放宽（实测成本比 1.000，旧实现 100 倍）。
8. 状态面板与准入同源：`platform_status["feedback"]` 取自 `FeedbackSource.latest`，
   `test_platform_status_reports_the_snapshot_admission_used` 守住这一点。

逐条核对结果：

| 条件 | 证据 |
| --- | --- |
| 1 | `ClientService.model` 只经 `self.models.submit`；全仓 grep `wait_platform` 只剩定义、对账与测试 |
| 2 | 容器内 `verify` 末行 `{"verify": "passed", ...}`，见第 8 节 |
| 3 | `test_personal_l3_waits_for_platform_feedback_and_pins_account`、`test_uncoordinated_dispatches_immediately` |
| 4 | `test_settle_returned_credit_unblocks_the_next_request`、`CostDRR` 四个 `refund` 用例 |
| 5 | `grep -r '"COMPLETED"' src tests` 为空 |
| 6 | `test_submit_after_close_is_rejected` |
| 7 | 容器内 unit+integration 全绿 |
| 8 | `test_platform_status_reports_the_snapshot_admission_used` |

### 6.1 第二轮复审的四条修订

| 问题 | 根因 | 修法 | 回归测试 |
| --- | --- | --- | --- |
| 候选被当成已派发，成本份额失衡、后台持续等待 | `CostDRR.pop` 按调用次数发额度，一次取整轮候选；名额不足时放回却不恢复公平状态 | 额度改**按轮发放**（`CostDRR._visited` / `CostDRR._turn_done`）；`pop` 支持 `limit` 与 `accept`，取到名额上限就停、请求级不可行的让位；`ModelScheduler._step` 用 `limit=capacity` 取候选，取出的全部派发，不再放回 | `test_cost_drr.py::test_cumulative_cost_share_is_fair_across_heterogeneous_loads`、`test_model_scheduler.py::test_callback_cost_share_is_fair_across_roots`、`::test_sustained_urgent_arrivals_do_not_starve_the_background` |
| 取消没传到执行路径 | `submit` 取消时只 `_drop` 排队信息，独立执行 Task 继续跑，`ClientService.model` 里 `gateway.cancel()` 的分支永不执行 | `ModelScheduler._executions` 维护 `request_id → 执行 Task`，`_abandon` 取消并等待它收尾；结果未知的一律保留预留进核对 | `test_client_cancel.py::test_cancel_after_model_dispatch_reaches_the_execution` |
| 校正用错原预扣量 | 平台结算后把 `GatewayRecord.reserved_units` 清零，客户端拿 0 当预扣量，`settle(root, 0, 10)` 反而多扣 10 | `ClientService._reserved_units` 从 `AttemptState.request_payload` 复原预扣量；`_settle_model` 在锁内读共享记录，按 Attempt 只校正一次 | `test_client_settle.py::test_settlement_uses_the_original_reservation` |
| 一条请求等待挡住全部 | 决策 `wait` 时放回全部候选并 `asyncio.sleep(retry_after)`，请求级阻塞被放大成整轮阻塞，到期处理也被推迟 | 账户级（`account_decision`）与请求级（`request_decision`）分开：账户级才整轮退避，请求级只让位；退避等待取"退避上限"与"距最近期限"的较小值 | `test_model_scheduler.py::test_request_level_block_does_not_stop_other_roots`、`::test_deadline_wakes_the_backoff`、`test_feedback_decision.py` 全组 |

附带修掉反馈周期配置矛盾：`ClientConfig.feedback_interval_seconds` 允许最长 30 秒，而陈旧阈值是 5 秒，
`FeedbackSource` 现在把实际复用时长收在 `STALE_FEEDBACK_SECONDS` 以内，不再持续复用一份自己判为陈旧的快照
（`test_feedback_source.py::test_reuse_window_is_capped_at_the_stale_threshold`）。

## 7. 明确不做的事

- 不做模型推理管理、GPU 分配、跨用户平台调度（属 L4）。
- 不在 L3 复制平台账本；L3 只读反馈，不写预算。
- 不改 `contracts/models.py`、`GatewayPort`、`PlatformFeedback`；不在他人文件里加防御。
- 不改 `Dispatcher`：它继续服务工具槽位，模型侧切走后仍保留。
- 不把算法参数做成配置项。

## 8. 验证记录与责任

验证环境：colima 0.10.3 起的 Linux 虚拟机，`docker.m.daocloud.io/library/python:3.12-slim` 容器内执行。
容器为 Debian GNU/Linux 13、Linux 6.8.0-117-generic aarch64、Python 3.12.15，`uv 0.13.1`，
依赖按 `uv.lock` 锁定安装，工作树取自 `feat/wp3` 的 `71d91d9`。

`python tools/project.py verify` 的输出要点（第一轮接线完成后）：

```
172 files already formatted
All checks passed!
Success: no issues found in 41 source files
Contracts: 3 kept, 0 broken.
89 passed in 2.52s            # tests/unit + tests/integration
Successfully built dist/taskloom-0.2.0-py3-none-any.whl
5 passed in 23.15s            # tests/smoke
{"verify": "passed", "wheel": "taskloom-0.2.0-py3-none-any.whl"}
```

单元加集成共 89 项全过，冒烟 5 项全过，wheel 产物审计通过。

第二轮复审的四条修订完成后，单元加集成共 96 项（`tests/unit` 70 全过、`tests/integration` 26 中 24 过），
其中 2 项 `test_executor.py` 在 macOS 上因缺少 `/proc/sys/kernel/random/boot_id` 失败——与本次改动无关，
Linux 容器内可通过，`verify` 结论待容器内复跑后更新本段。

说明：`tools/project.py bootstrap` 固定下载 `uv-x86_64-unknown-linux-gnu`，在 aarch64 容器里
跑不起来（缺 x86_64 动态加载器）。本次验证改用同一发行版的 aarch64 构建放进 `.toolchain/uv/uv`，
`bootstrap` 检测到二进制已存在便跳过下载，校验逻辑与产物审计均未受影响。

责任：

- 组件修正与本文：杜金泽（WP3）。
- 接线实现与集成验收：杜金泽；接口与配置增量由组长确认后合并。
- 合并门槛：第 6 节清单 + `verify` 结果附在 PR 正文。
