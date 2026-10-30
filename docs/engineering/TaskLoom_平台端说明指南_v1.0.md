# TaskLoom 平台端说明指南

**版本**：v1.0 · **核对日期**：2026-10-30 · **状态**：正式实现

TaskLoom 是一套由个人端 Agent 客户端与统一模型服务平台组成的协同调度系统。本指南系统化梳理 TaskLoom **平台端（及其与个人端交互的模型请求层 L3—L4）** 的功能实现与底层操作系统（OS）经典算法的映射关系，为技术理解、架构审查与实验复现提供权威参考。

---

## 1. 架构层次与功能算法全景

TaskLoom 将模型推理调度划分为客户端边缘层（L3）与平台中心层（L4）：
* **L3 个人模型请求管理（边缘协同层）**：运行于用户本机，统管单个用户的多个根任务对模型 API 的并发调用，负责本地防饥饿、成本整形与平台背压感知。
* **L4 平台多租户调度与网关（平台中心层）**：运行于 TaskLoom 平台服务，统管所有认证租户对固定模型服务池（Model Pool）的竞争，负责硬额度准入、不可抢占槽位分配、多账号缓存亲和性与权威账本审计。

| 层次 | 功能模块 | 对应 OS 经典概念与算法 | 核心源文件 |
|---|---|---|---|
| **L3** | 多根任务服务份额分配 | **成本赤字轮转（Cost DRR）** | `src/taskloom/policies/fair.py`<br>`src/taskloom/client/model_scheduler.py` |
| **L3** | 长尾请求防饥饿与超时控制 | **优先级老化与最早截止时间优先（Aging & EDF）** | `src/taskloom/policies/aging.py`<br>`src/taskloom/client/model_scheduler.py` |
| **L3** | 平台状态感知与协同退避 | **基于显式反馈的拥塞控制（Explicit Congestion Notification / Backpressure）** | `src/taskloom/client/feedback_decision.py`<br>`src/taskloom/client/feedback_source.py` |
| **L4** | 多租户不可抢占槽位调度 | **分层瞬时最大最小公平（Hierarchical Instantaneous Max-Min）与混合调度（Hybrid）** | `src/taskloom/policies/scheduling.py`<br>`src/taskloom/gateway/service.py` |
| **L4** | 多上游账号拓扑与提示词缓存 | **缓存亲和性调度（Cache Affinity）与延迟调度（Delay Scheduling）** | `src/taskloom/policies/placement.py` |
| **L4** | 额度准入与预算超额预防 | **悲观边界准入与自适应预留（Worst-case Bound & EWMA Estimation）** | `src/taskloom/policies/reservation.py`<br>`src/taskloom/gateway/service.py` |
| **L4** | 事务状态机与故障恢复 | **预写日志（WAL）与幂等恢复状态机（Idempotent State Machine）** | `src/taskloom/adapters/sqlite.py`<br>`src/taskloom/gateway/service.py` |
| **L4** | 控制台与运行态管理 | **控制面与数据面分离（Control/Data Plane Separation）与策略热切换** | `src/taskloom/web/admin/`<br>`src/taskloom/gateway/service.py` |

---

## 2. L3 层：个人模型请求管理（边缘协同层）

### 2.1 功能：多根任务成本份额分配
* **对应 OS 算法**：**成本赤字轮转（Cost Deficit Round Robin, Cost DRR）**
* **应用场景**：同一用户可能并发运行多个目标迥异的根任务（如前台代码编写交互、后台批量长文本扫描）。传统以“请求个数”轮转的 Round-Robin 会使长请求霸占模型额度，导致短交互延迟剧增。
* **原理说明**：
  1. 借鉴网络数据包调度的 Deficit Round Robin 机制，L3 调度器以“计费代价单位”（由输入/输出 Token 估算）作为配额单位。
  2. 为每个活跃根任务分配一个赤字计数器（Deficit Counter）。每轮轮转遍历活跃队列时，各任务累加一个固定的量子（Quantum）。
  3. 队列仅在当前任务的 $\text{Deficit} \ge \text{charge\_units}$ 时才准许出队并派发；派发后扣除实际占用的赤字值。若不足则保留赤字并移交轮转权。
  4. 任务全部请求处理完毕离开队列时，其累积赤字直接清零（No Idle Credit），防止闲置任务重新上线时形成突发性垄断。
* **复现操作**：
  ```bash
  # 运行 L3 模型调度器单元测试（验证赤字递增、扣减与让位）
  uv run --locked pytest tests/unit/test_model_scheduler.py -k "test_drr or test_cost"
  # 验证客户端多任务调度集成行为
  uv run --locked pytest tests/integration/test_client_service.py
  ```

### 2.2 功能：长尾请求防饥饿与超时控制
* **对应 OS 算法**：**动态优先级老化（Aging）与最早截止时间优先（Earliest Deadline First, EDF）**
* **应用场景**：在高负载或交互任务持续提交时，普通或低优先级后台任务容易出现长时间饥饿；同时，已超时或被用户取消的无意义请求若继续流向平台，将空耗平台昂贵的模型算力。
* **原理说明**：
  1. **老化（Aging）**：在 `AgingQueue` 中，所有待派发请求记录到达时间。当请求在队列内排队时间超过设定的老化窗口（`aging_window`）时，系统动态提升其有效优先级，确保低优先级请求在有限等待后必定能获得调度机会。
  2. **绝对期限拦截（Deadline Enforcement）**：每个请求显式携带 `deadline` 绝对时间戳。调度主循环以当前队首最小 deadline 计算休眠唤醒时间。一旦当前时间超过 deadline，请求直接在 L3 终止，标记为终态错误码 `deadline_expired` 并释放本地在途名额，坚决不向平台发起无效网络请求。
* **复现操作**：
  ```bash
  # 验证老化机制与截止期超时触发
  uv run --locked pytest tests/unit/test_model_scheduler.py -k "test_aging or test_deadline"
  ```

### 2.3 功能：平台状态感知与协同退避
* **对应 OS 算法**：**基于显式反馈的拥塞控制与流量整形（Explicit Congestion Notification / Backpressure）**
* **应用场景**：平台后端模型池容量耗尽排队、或者用户在平台的滑动窗口速率（Rate Limit）与总预算耗尽时，客户端若盲目以固定频率重试，会产生网络冲击与惊群效应（Thundering Herd）。
* **原理说明**：
  1. L3 通过独立轻量接口周期性获取平台的只读状态快照 `PlatformFeedback`，包括平台总忙碌槽位、租户已用在途数、当前速率窗口余量及平台计算出的推荐退避时长 `suggested_wait_seconds`。
  2. 调度器在将请求移出队列前，通过 `account_decision` 与 `request_decision` 进行前置门禁：
     * **容量超载或速率窗口耗尽**：请求保留在 L3 队列中，主动休眠退避，不消耗网络带宽与平台排队队列。
     * **硬预算不可逆耗尽**：立即在本地抛出拒绝异常（`Refused("insufficient_budget")`），阻断无效派发。
* **复现操作**：
  ```bash
  # 验证客户端在背压与额度耗尽时的等待/拒绝策略
  uv run --locked pytest tests/unit/test_feedback_decision.py
  uv run --locked pytest tests/unit/test_feedback_source.py
  ```

---

## 3. L4 层：平台端多租户网关与请求调度中心

### 3.1 功能：多租户不可抢占槽位分配
* **对应 OS 算法**：**分层瞬时最大最小公平调度（Hierarchical Instantaneous Max-Min Fairness）及混合策略（Hybrid）**
* **应用场景**：平台持有固定并发容量的模型池（如 16 个并发槽位）。模型生成具有**不可抢占性**（一旦派发无法暂停，必须等其完成或主动取消并结算），且输出 Token 长度方差极大（短请求 1 秒，长推理请求可能耗时 100 秒以上）。简单 FIFO 会导致单租户的长请求霸占所有槽位（Decode-heavy 垄断），造成交互式短任务严重拖尾。
* **原理说明**：
  1. **分层两级结构**：外层调度在不同的注册租户（Tenant）之间裁决，内层调度在同一租户名下的各根任务（Root Task）之间裁决。
  2. **瞬时 Max-Min（`maxmin`）**：定义公平标准为“在当前时刻有人排队时，各活跃租户所占用的槽位数是否均衡”。每当空出一个槽位，优先派发给当前持有槽位数最少的租户；平局时再分配给最久未被服务的租户。
  3. **混合调度策略（`hybrid`，推荐生产候选）**：
     * **第一关键字**：比较当前瞬时占槽数（Held Slots），落实 Max-Min 思想，优先抬升槽位少的租户；
     * **第二关键字（平局仲裁）**：比较近期消耗的累计槽位秒（`slot-seconds`），融入 DRR 最少已得服务原则，防止突发流量在平局时借用不公。
* **复现操作**：
  ```bash
  # 运行平台调度策略单元测试
  uv run --locked pytest tests/unit/test_scheduling_policy.py
  # 执行 L4 基准评测套件，对比基准 rr 与 hybrid 策略的瞬时公平与 SLO 达成率
  uv run --locked python tests/l4/bench.py run --quick --policy hybrid
  uv run --locked python tests/l4/bench.py run --quick --policy rr
  ```

### 3.2 功能：多上游账号拓扑与提示词缓存管理
* **对应 OS 算法**：**缓存亲和性调度（Cache Affinity Scheduling）与延迟调度（Delay Scheduling）**
* **应用场景**：平台的固定模型池通常聚合了多个上游 API 账号，每个账号在服务端维护独立的 Prompt Cache。同一个 Agent 链条的上下文往往包含长篇历史对话；若将其后续请求调度到未曾使用过的冷账号，Prompt Cache 命中率从 93% 暴跌至 12%，直接引发推理延迟暴涨与额外的 Token 费用。
* **原理说明**：
  1. **多账号缓存记忆（AffinityPlacement）**：网关维护 LRU 映射表（`MAX_CHAINS = 8192`），记录每条链（`chain = (tenant, agent_id)`）在历史上游账号的最近使用时刻。派发时优先选择拥有最热缓存（使用时间最近）且有空闲槽位的上游账号；若历史账号不可用，则回退到当前槽位利用率最低（`busy / capacity` 最小）的账号。
  2. **延迟调度（Delay Scheduling）**：若最近服务过该链的热账号当前无空槽，且配置了 `cache_wait_seconds > 0`，网关允许该请求暂时让出派发机会（最多等待 `cache_wait_seconds`），等待热账号释放槽位；仅在等待超时后，才降级分配给其他空闲账号。
* **复现操作**：
  ```bash
  # 验证亲和性放置与延迟判定逻辑
  uv run --locked pytest tests/unit/test_scheduling_policy.py -k "placement or affinity"
  # 运行 Benchmark 测量多账号放置下的缓存保留率与成本节省
  uv run --locked python tests/l4/bench.py run --quick --placement gateway
  ```

### 3.3 功能：额度准入与预算超额预防
* **对应 OS 算法**：**悲观上界准入（Worst-case Bound）与基于 EWMA 的两阶段资源分配（Two-Phase Allocation）**
* **应用场景**：在模型推理完成之前，模型的真实生成 Token 数与实际 Prompt 缓存命中率是未知的。如果完全按预估值准入，遇到长输出时可能击穿租户硬预算或速率窗口；如果全程按最坏值记账，则会导致大量额度被虚假冻结。
* **原理说明**：
  1. **第一阶段：悲观硬门禁（Worst-case Bound）**：
     通过 `worst_units = input_tokens * max(1.0, cache_price) + max_tokens * output_price` 计算最坏情况下的最大可能消耗。在接纳请求时，严格校验 `worst_units` 是否超出租户总预算及当前 60 秒滑动窗口上限。任何超额请求均会被直接拒绝，保证系统永不透支（No Overcommit）。
  2. **第二阶段：自适应软预留（EWMA Reservation）**：
     在调度派发前，`ReservationEstimator` 利用指数加权移动平均（EWMA）结合历史均值偏差预测实际消耗，将预估值记为当前请求的在途预留（Reservation），更新至平台共享账本。
  3. **终态对齐（True-up）**：
     在上游返回实际消耗后，按供应商实际用量结算，退还多余预留，实现两阶段原子闭环。
* **复现操作**：
  ```bash
  # 验证悲观准入与 EWMA 预估计算
  uv run --locked pytest tests/unit/test_reservation_policy.py
  uv run --locked pytest tests/l4/test_reservation.py
  ```

### 3.4 功能：事务边界、故障恢复与幂等结算
* **对应 OS 算法**：**预写日志（Write-Ahead Logging, WAL）与幂等状态机（Idempotent State Machine）**
* **应用场景**：平台进程在向模型池发起 HTTP 派发过程中突发宕机、网络超时或客户端重复提交。若缺乏可靠的恢复机制，会导致资源账本泄露、扣费重复或未决请求死锁。
* **原理说明**：
  1. **派发意图前置记录**：在向模型池发出网络请求之前，网关必须在 SQLite WAL 事务中持久化 `DISPATCH_INTENT` 事件与初始预留状态。
  2. **未知状态安全隔离**：若网络发生断连或收到非预期的 503 错误，请求不直接重发，而是进入 `UNKNOWN/RECONCILING` 状态，完整保留对应预留，阻止未对账的重复消费。
  3. **重启幂等扫描与恢复**：平台重启时，自动扫描未决记录，重新载入各租户的在途槽位计数与预算占用；相同 `request_id` 的重复提交自动命中幂等索引，直接返回原结果。
* **复现操作**：
  ```bash
  # 验证网关服务在并发、故障与重复提交下的幂等处理
  uv run --locked pytest tests/integration/test_gateway.py
  # 验证平台存储与账本事务一致性
  uv run --locked pytest tests/l4/test_kit.py
  ```

### 3.5 功能：控制台与运行态管理
* **对应 OS 算法**：**控制面与数据面分离（Control/Data Plane Separation）与动态策略重配置（Live Policy Hot-switching）**
* **应用场景**：在生产和实验运行中，平台管理员需要实时观察队列积压原因（`blocked_by`）、全局槽位占用率，并需要在线动态切换不同的调度策略以观察对比效果，且该运维操作绝不能影响正在进行的大模型请求通信。
* **原理说明**：
  1. **通道分离**：管理端点（`GET /admin/v1/scheduler`、`POST /admin/v1/scheduler/settings`）使用独立的管理员密钥（`admin_key`）与独立查询路径，仅读取必要指标与事件环形缓冲区（最后 200 条调度事件），不涉及请求正文的反序列化。
  2. **在线无损热切换**：调用热切接口修改 `scheduling_policy`（如从 `rr` 切换至 `hybrid`）时，网关在持有轻量调度锁的情况下，将当前排队队列中的请求按准入时间戳无损注入新策略的数据结构，在途运行中的请求继续维持其计时与预留，修改持久化至配置并触发审计事件。
* **复现操作**：
  ```bash
  # 自动化测试控制台读取与热切换接口
  uv run --locked pytest tests/integration/test_admin_console.py
  # 启动独立的平台控制台回放演示
  uv run --locked python tests/l4/console_demo.py
  # 浏览器访问 http://127.0.0.1:8796/admin 查看控制台并体验热切功能
  ```

---

## 4. 统一验证与实验操作手册

为确保各算法的正确性与系统稳定性，平台端提供从微观单元到宏观回放的完整验证链路：

| 验证阶段 | 目标范围 | 推荐执行命令 |
|---|---|---|
| **代码与契约检查** | 格式、类型与分层架构依赖 | `python3 tools/project.py check` |
| **算法单元验证** | L3/L4 调度器、亲和性、预留算法 | `uv run --locked pytest tests/unit/test_scheduling_policy.py tests/unit/test_model_scheduler.py tests/unit/test_reservation_policy.py` |
| **网关集成验证** | 账户鉴权、预留与控制台接口 | `uv run --locked pytest tests/integration/test_gateway.py tests/integration/test_admin_console.py` |
| **平台脱机仿真** | 多租户场景模拟与违规审计 | `uv run --locked python tests/l4/simulate.py --users 4 --tasks 6 --capacity 2` |
| **调度性能 Benchmark** | 测量公平性、SLO 与缓存未命中 | `uv run --locked python tests/l4/bench.py run --quick --policy hybrid` |
| **控制台全功能演示** | 真实/合成负载回放与可视化控制台 | `uv run --locked python tests/l4/console_demo.py` |
