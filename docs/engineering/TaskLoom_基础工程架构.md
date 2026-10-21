# TaskLoom 基础工程架构

**更新日期** 2026-10-21 · **实现版本** 0.2.0 · **产品方案** [当前完整说明书](../TaskLoom_项目说明书_当前完整方案.md)

本文记录 0.2 已合并代码的实际结构，依次说明统一系统模型、模块与进程、协议及账本、执行与恢复。待合并能力、后续算法与实际验证见[当前实现与开发状态](TaskLoom_当前实现与开发状态.md)。

## 1. 系统与运行拓扑

TaskLoom 的个人端和平台端属于同一系统。用户向平台注册账号，个人端使用该账号 Key 调用本平台。平台提供统一模型服务池，账户额度、全局容量和请求队列由平台权威管理。

| 进程 | 所属端 | 职责 |
|---|---|---|
| client。 | 每位用户的个人端。 | 运行轻量 Agent，维护任务与依赖、L1 准入、L3 请求调度，提供本地界面。 |
| executor 与工具子进程。 | 每位用户的个人端。 | 通过私有 Unix socket 接收工作，启动工具、排空输出、取消并核对真实退出。 |
| supervisor。 | 每位用户的个人端。 | 独立启动、监督和重启 client/executor，支持 daemon，使用自己的控制资源。 |
| platform 与 model-pool。 | 统一平台。 | 前者提供账户、L4 与账本；后者表示平台内部固定模型能力。 |

开发启动将 Alice、Bob 两套个人端放在同一 WSL 内，以不同 profile、端口与状态目录模拟两个用户。生产边界允许平台独立主机部署；当前默认监听 loopback，独立部署由 PlatformConfig.listen_host 配置监听地址。

```mermaid
flowchart LR
    U[用户] --> UI[本地浏览器界面]
    UI --> C[client：L2 / L3 / L1 准入]
    C -->|Unix socket| E[executor]
    E --> T[本地工具进程]
    C -->|平台账号 API Key| P[platform：账户与 L4]
    P -->|平台内部密钥| M[固定模型池]
    P -->|账户额度与容量反馈| C
```

模型池是固定依赖。当前用固定并发槽位和模拟响应时间表征容量，L4 只调度模型服务之前的准入与派发；Linux 调度本地工具进程，模型配置在实验中保持固定。

## 2. 模块与配置所有权

### 2.1 个人端

runtime/agent.py 通过 model、tool、checkpoint 三个端口推进当前已揭示步骤。client/service.py 管理 Task/Agent/Step/Attempt、输入版本、任务预算、取消代次和成果。client/dispatcher.py 使用纯候选策略和原子资源池安排模型及工具工作。

L1 目前预留工具名额、内存、产物空间。adapters/executor.py 记录启动意图、进程身份和结果；adapters/linux.py 设置 cgroup 内存、CPU 和进程数限制。输出读取线程持续排空 stdout/stderr，并限制保留量。

L3 在实际发送前读取账户反馈。开启 coordinated 时，账户排队、窗口或预算暂不可用会转为可取消等待；关闭该开关只改变等待策略，仍然使用同一平台。

ClientConfig 只包含平台 URL、profile 和个人资源/调度配置。实际账号 Key 位于本地 platform-credentials.json，权限为 0600。profile 的 platform_id/account_id 绑定另存账本，不同身份使用独立 profile。

个人端工作台将 Conversation 作为交互容器，同一会话可包含多个独立根任务。每条消息以 TaskCreate.instruction 和输入文件保存，助手结果直接读取任务的校验产物，执行路径读取 Attempt。会话归档只控制后续消息准入，运行中的任务继续执行；取消通过独立任务操作发起。历史无会话任务在启动时原子补建会话记录。

### 2.2 平台端

gateway/accounts.py 管理账号、密码验证、Key 签发和撤销。账户额度由平台注册策略设置；平台保存加盐 PBKDF2 密码验证值和 API Key 摘要，同一账号的多个 Key 共享一份账户额度。

gateway/service.py 运行 L4 调度。外层按账号、内层按根任务轮转，派发时同时检查根任务预算、账户与全局的累计预算/窗口/在途量。平台池容量由 PlatformConfig.pool_capacity 固定。

adapters/provider.py 是平台内部模型服务连接。账户请求 ID 映射为账号隔离的内部身份，再调用固定模型池。模型池还验证内部密钥和容量；个人账号 Key 无法直接调用池。

PlatformConfig 持有模型池 URL、内部密钥、模型名称、总服务容量和注册账户限额。PoolConfig 定义模拟池的容量、延迟、模型和内部密钥。

### 2.3 共享工程边界

domain 和 policies 使用标准库；contracts 使用 Pydantic 定义跨边界数据；业务模块依赖端口，adapters 实现协议，bootstrap 显式装配。三项 import-linter 约束执行依赖方向。所有角色由同一个 Python distribution 发布，版本以 pyproject.toml 为准。

## 3. 接口、反馈与持久化

### 3.1 接口分组

| 接口组 | 路由 | 作用 |
|---|---|---|
| 平台账号。 | /v1/accounts/register、login；/v1/account；/v1/account/keys/{id}/revoke。 | 签发、验证、撤销平台账号 Key，读取本人额度和反馈。 |
| 平台请求。 | /v1/roots；/v1/requests；请求查询与 cancel；/v1/models。 | 登记任务归属，接纳、查询和取消模型请求。 |
| 个人端。 | /api/v1/platform；register、login、connect；/api/v1/tasks、graph、cancel、resume、artifacts。 | 配置本平台账号，推进完整任务，查看依赖与交付。 |
| 执行与观测。 | Unix socket 上的 /execute、/attempts；HTTP /health/live、ready、/api/v1/status。 | 管理工具执行，检查进程服务和平台容量。 |

客户端 POST 需要 X-TaskLoom-Local: 1，平台账号路由以账号 API Key 鉴权。模型池私钥只在平台内使用。普通账号反馈排除其他用户的任务、凭证和密码。

会话接口为 /api/v1/conversations、/{id}、/{id}/update 和 /{id}/messages，分别用于列表与创建、详情、重命名或归档、提交新任务。会话及根任务关联在同一事务中持久化。/api/v1/resources 另外提供模型名额与逐 Attempt 资源分配；内存数值表示调度预留，文件空间表示实际文件总量。平台反馈显式提供模型池类型和计量单位。

### 3.2 两级额度与反馈

平台全局账本记录总消费、总预留、总在途与窗口用量；每个账号有对应的独立账本。请求派发时同时预留两级额度；结算时按一次实际消费更新两级账本。根任务预算是该账号之下的进一步约束。

PlatformFeedback 包含 platform_id、account_id、可用模型、账户消费/预留/剩余、账户在途/排队、共享池占用/容量、发送建议、重试时间和观察时间。L3 每次决策查询当前反馈，平台始终执行最终准入。当前推荐待处理上限为每账号一个排队请求，是基础背压策略。

### 3.3 状态与升级

个人端数据库为 clients/<profile>/client.db；平台为 platform/state.db；模拟池为 platform/pool.db。SQLite WAL 与单写入线程保存记录和事务事件。执行器另保存 Attempt 原子 JSON 日志。

0.1 的 gateway/state.db 与 provider/state.db 在首次启动时复制到新平台目录，原文件保留。复制前取得旧拥有者锁，已存在的新账本保持原样。任务输入、成果、执行日志和旧凭证配置不做删除。

## 4. 执行、取消与扩展

个人端保存取消意图后，停止排队/运行工作；工具资源以真实退出为依据归还，未确认执行保持隔离预留。任务结果、物理停止和费用结算分别记录，旧代次结果只保留相应消费和执行事实。

平台在网络调用前保存 DISPATCH_INTENT。状态未知时保留两级预留并核对原请求，重复传输不产生新的派发。当前模拟池支持按请求查询；fixed-http 连接的核对合同需由相应模型服务提供。

supervisor 自动恢复个人端服务。平台目前通过角色启动和持久恢复工作，平台进程的部署监督由后续运行环境接入。

高级调度在当前基础上分项实现：成本 DRR 与老化进入两级队列，阶段银行家进入受控资源申请，多工具与子 Agent 提供真实竞争负载，自适应反馈基于当前账户反馈协议继续实验。
