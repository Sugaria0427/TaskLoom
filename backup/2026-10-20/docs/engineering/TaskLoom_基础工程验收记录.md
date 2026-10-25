# TaskLoom「织序」基础工程验收记录

> 本文保留 0.1 基线的历史验收。当前系统已按统一平台账号与固定模型池改造为 0.2，最新结果见[统一系统改造与验收](TaskLoom_统一系统改造与验收.md)。

**验收日期** 2026-10-15 · **软件版本** 0.1.0 · **范围** 工程计划 P0—P2。

本记录依次说明交付环境、实际实现、验证证据和使用方式。项目的完整研究目标见[当前完整方案](../TaskLoom_项目说明书_当前完整方案.md)，代码职责见[基础工程架构](TaskLoom_基础工程架构.md)，后续阶段见[工程搭建计划](TaskLoom_工程骨架搭建计划.md)。

本次交付建立可继续开发的执行底座。开发闭环使用本地可控模型 HTTP 服务，工具执行、文件统计、资源组、持久账本和守护进程均实际运行。成本 DRR、阶段银行家、子 Agent 扇出、动态反馈、真实模型与 OpenClaw 对照归入 P3。

## 1. 环境与交付

### 1.1 本机环境

| 项目 | 实际结果 |
|---|---|
| 工作区与版本管理。 | E:/Project/OS-Project；源码在 WSL 中对应 /mnt/e/Project/OS-Project。本地 Git 已初始化，分支为 main。 |
| 产品运行环境。 | Ubuntu 24.04.1 / WSL2，Linux 6.6.87.2，x86_64，Python 3.12.3。Windows 负责编辑、开发命令入口和浏览器访问。 |
| 安装工具与依赖。 | 项目内 uv 0.13.1，下载后校验固定 SHA-256；uv.lock 锁定依赖。虚拟环境与运行状态默认位于 WSL 用户目录。 |
| OS 资源保护。 | systemd 用户 scope 可委派 cgroup v2 的 cpu、memory、pids 控制器；开发驱动和 Linux 测试使用临时委派域。 |

安装、启动与验收均使用当前用户权限。各服务默认绑定 127.0.0.1，开发模型、网关、客户端分别使用 8797、8796、8795。普通 doctor 运行在委派域之外，因此 delegated_root 显示 null；dev 与 test linux 在启动时创建各自的委派域。

### 1.2 产品与运行数据

产品源代码位于 src/taskloom，产品开发工具位于 tools，开发配置位于 configs/dev。研究、正式说明与文档编写工具沿用现有目录。

默认环境为 /home/sugaria/.cache/taskloom/venvs/7a8a0e41b4，默认状态为 /home/sugaria/.local/share/taskloom/7a8a0e41b4。这些路径由工作区计算，迁移目录后以 doctor 输出为准。

安装包为 taskloom-0.1.0-py3-none-any.whl，另提供 sdist。wheel 包含产品模块和静态页面；依赖及运行数据各自在安装与启动时生成。

## 2. 实现与工程约束

### 2.1 两端任务闭环

| 边界 | 已实现行为 | 可观察结果 |
|---|---|---|
| 个人端 L1。 | 原子预留工具名额、内存和产物预算，启动独立工具进程；大需求超过总容量时拒绝准入。 | 文件统计、哈希计算、受控并发、实际停止和资源归还。 |
| 个人端 L2。 | 有界 Agent 循环，记录 Task、Agent、Step、Attempt、显式前驱和检查点；核对输入版本与成果字段。 | 完成模型—工具—模型链路，发布可读取的 JSON 成果。 |
| 个人端 L3。 | 根任务轮转、私有预算和模型在途限制；重传保留请求身份，续作生成新的执行尝试。 | 个人任务共享请求名额，用量和待核对预留分别展示。 |
| 平台端 L4。 | 认证用户，登记根任务，先按用户再按根任务轮转；有界排队、窗口额度、共享预算与派发核对。 | 重复提交幂等处理、跨用户隔离、过期排队清理、未知派发保留预留。 |

当前注册工具为 file_stats，资料按空白分词，适合核对固定夹具。工具进程真实读取输入文件、执行可配置哈希工作并输出产物。可控模型生成确定的工具调用和摘要；mock_microcredit 是测试计量单位。

### 2.2 OS 机制与一致性

| 机制组 | 实现要点 |
|---|---|
| 调度与同步。 | asyncio 推进任务，根任务轮转选择可准入工作；短临界区保护预算和资源向量，数据库写入由单独线程串行处理。 |
| 进程与通信。 | supervisor、client、executor、gateway、mock-provider 独立运行；本地执行通过私有 Unix socket 通信，工具输出通过读取线程持续排空并截断保留。 |
| 保护与回收。 | 每次工具尝试使用独立进程组和 cgroup；设置 CPU、内存、swap 与进程数限制；许可依据实际停止事实释放。 |
| 持久与恢复。 | SQLite WAL、原子记录与事件事务、工具启动日志和 PID/启动时间/boot 身份核对；未知派发先核对，旧代次结果保持原有发布归属。 |

任务与 Agent 的初始身份同事务提交；步骤与尝试同事务登记。取消先记录意图，再停止执行；排队中尚未派发的尝试持久化为 CANCELLED。成果发布、物理停止和费用结算分别记录。

### 2.3 开发约束

领域和策略使用标准库，业务模块通过端口依赖适配器，bootstrap 负责装配。import-linter 执行三项依赖约束；mypy 严格检查产品代码。格式、lint、类型、测试和打包均从 tools/project.py 进入。

根 AGENTS.md 规定身份归属、预算、取消、重试、恢复和文件政策。配置通过 TOML、环境变量和命令行边界解析；密钥由环境变量提供，开发用 token 在配置中明确标识。

## 3. 验证结果

### 3.1 静态与行为验证

| 验证组 | 实际覆盖 | 结果 |
|---|---|---|
| 静态检查。 | Ruff 格式和 lint、mypy 严格类型、三项模块依赖约束。 | 通过；mypy 覆盖 35 个产品源文件。 |
| 领域与集成。 | 原子资源、根任务份额、SQLite 回滚和事件幂等、额度和用户隔离、未知派发、真实工具输出、取消与输入版本。 | 21 项通过，包括排队取消的持久状态回归。 |
| 完整进程闭环。 | 正常交付、执行中取消与续作、输入变更阻断、SIGKILL 后监督重启且工具保持一次执行。 | 三项 smoke 通过。 |
| Linux 专项。 | 实际 OOM、daemon 脱离与重启、完整任务使用真实 cgroup 并回收。 | 三项通过。 |

内存实验将工具组限制为 32 MiB，并实际触碰 128 MiB 分配页面；观察 SIGKILL 和 oom_kill 计数后检查组清理。输出实验写出超过 3 MiB 的 stderr，验证持续排空且仅保留 1 MiB。

### 3.2 干净环境与安装包

verify-isolated 创建临时产品源码副本，排除原工作目录中的缓存和运行状态；在临时目录内创建新环境，执行 locked 安装、canonical verify 和包内容检查。随后把 wheel 及锁定的运行依赖安装到另一环境，从源码目录之外执行版本查询和 self-test。

最终代码已通过完整独立安装验证：两个文件、五个词，形成一个 Agent、三个 Step、三个 Attempt 和两项显式前驱关系；结算后预留为零。安装后 self-test 消费 553 个模拟单位。临时源码、环境和状态随验证结束回收。

原始输出见 [verify-isolated.log](../../agent-workspace/outputs/engineering/20261015/verify-isolated.log) 与 [test-linux.log](../../agent-workspace/outputs/engineering/20261015/test-linux.log)。

### 3.3 浏览器验收

使用 agent-browser 操作实际个人端页面，完成提交、取消、核对并续作和成果展示。正常资料任务核对一个文件、五个词；页面读取真实资源快照与事件。浏览器错误记录为空。

390 × 844 移动视口中，页面 scrollWidth 与 innerWidth 均为 390，表单和资源卡片按单列布局显示。截图和原始检查输出保存于 [20261015 验收输出](../../agent-workspace/outputs/engineering/20261015/)。

### 3.4 独立接手

独立 Agent 在新的上下文中，仅凭仓库路径和“接管并验证”的任务，从 README、AGENTS 和开发驱动找到执行入口。它独立完成 doctor、bootstrap、verify、test linux、verify-isolated，各命令退出码均为零。

接手验收另用独立状态目录及 8895/8896/8897 端口启动 dev，实际提交两个文件并读取成果、依赖、资源和事件接口。一个 Agent、三个 Step 和三个 Attempt 均可查询，最终预留为零，工具的 cgroup 已回收。

本机复测耗时：verify 约 50.5 秒，Linux 专项约 14.5 秒，独立安装约 36.3 秒；独立 HTTP 任务约 0.8 秒。以上是验收记录，用于复现当前夹具。命令、时间与返回结果见 [cold-handoff 输出](../../agent-workspace/outputs/engineering/20261015/cold-handoff/)；典型改动的代码、测试和文档入口见第 4.2 节。

独立接手结论为通过，无本机可复现阻塞。验收拓扑已通过统一 stop 结束，三个专用端口均关闭，自有服务与工具进程已退出。具体结论见[接手验收](../../agent-workspace/outputs/engineering/20261015/cold-handoff/接手验收.md)，清理证据见[07-cleanup.json](../../agent-workspace/outputs/engineering/20261015/cold-handoff/07-cleanup.json)。

## 4. 使用与后续

### 4.1 本机启动

在项目根目录的 PowerShell 中执行：

~~~powershell
python tools/project.py doctor
python tools/project.py bootstrap
python tools/project.py dev
~~~

浏览器打开 http://127.0.0.1:8795。使用表单提交资料；选择增加哈希计算可以观察执行和取消。网关状态在 http://127.0.0.1:8796/api/v1/status，各 HTTP 服务的 /docs 提供接口说明。

Ctrl+C 结束前台开发拓扑，另一个终端可执行 python tools/project.py stop。启动前检查端口占用；端口覆盖参数为 --port、--gateway-port、--provider-port。

### 4.2 后续开发入口

| 开发主题 | 首先修改 | 验证入口 |
|---|---|---|
| 工具与任务画像。 | execution/tool.py、runtime/agent.py、execution/ports.py 与 contracts。 | 定向 integration，随后 smoke；涉及进程保护时增加 test linux。 |
| 请求与公平策略。 | policies/fair.py、client/dispatcher.py、gateway/service.py。 | unit 与额度/幂等 integration。 |
| 资源与可靠性。 | adapters/executor.py、adapters/linux.py、adapters/supervisor.py。 | 取消/恢复 integration 和 Linux 专项。 |
| 展示与实验。 | web、bootstrap/apps.py、tests/smoke。 | 实际浏览器流程与相同配置下的任务测量。 |

下一阶段先补充多工具与子 Agent 场景，再增加成本 DRR、阶段银行家和反馈消融。固定当前基线与相同工作负载，测量完整任务交付、等待、公平性及管理开销，并接入真实模型与独立 OpenClaw 对照。

### 4.3 工作区状态

现有资料和文档保留。本轮初始化本地 Git，代码与文档处于未提交状态。依赖、运行数据、构建产物及验收临时输出由忽略规则隔离；文档链接、忽略规则和构建产物已核对。验收服务已停止，按第 4.1 节可重新启动开发。
