# TaskLoom 统一系统改造与验收

**日期** 2026-10-15 · **软件版本** 0.2.0 · **状态** 通过本机与独立安装验收。

本记录说明统一系统模型、实际代码变化、验证结果和升级方式。产品总体方案见[当前完整说明书](../TaskLoom_项目说明书_当前完整方案.md)，模块及协议见[基础工程架构](TaskLoom_基础工程架构.md)。

本轮验收覆盖账号、两端执行、共享服务容量与恢复正确性。固定模型池为可控仿真，真实文件工具和 OS 资源保护实际运行；成本 DRR、银行家、子 Agent 和性能增量研究继续按工程计划 P3 开展。

## 1. 统一系统模型

TaskLoom 由个人客户端和模型服务平台共同组成。用户在平台注册账号，个人端使用该账号的 API Key。模型请求统一经过平台 L4，平台内部以固定模型池提供相同服务能力。

| 管理域 | 权威拥有者 | 本轮实现 |
|---|---|---|
| 本机任务与工具。 | 个人端 L1/L2。 | 任务、步骤、尝试、输入/成果、真实工具进程、资源保护与监督恢复。 |
| 本人的模型请求。 | 个人端 L3。 | 根任务轮转、私有预算、在途限制和依据平台反馈等待。 |
| 账户与共享模型机会。 | 平台 L4。 | 平台账户认证、账户/全局额度、用户/根任务队列及幂等结算。 |
| 平台固定模型池。 | 平台内部服务。 | 固定容量、内部密钥、实际在途与峰值、结果凭据查询。 |

关闭 coordinated 只关闭个人端反馈等待，平台鉴权、配额与模型路径保持一致。浏览器提供个人端界面，本机 Python 服务执行 Agent 与工具。

## 2. 代码与文档变化

### 2.1 账号与配置

新增 gateway/accounts.py，提供账号注册、密码登录、平台 API Key 签发与撤销。平台账户库使用加盐 PBKDF2 验证密码，Key 以摘要保存。普通账号由平台注册策略分配额度，多把 Key 共享同一账号限额。

RuntimeConfig 拆为 ClientConfig、PlatformConfig、PoolConfig。个人端配置只包含本平台地址与本机设置，账号 Key 保存到权限 0600 的私有文件。平台独占模型池地址、内部密钥、模型和总容量。

开发启动通过真实注册接口初始化 Alice、Bob。为开发断点恢复，随机开发账户密码保存在私有 platform/development-accounts.json；业务账户库保存密码验证值。该文件与模型池密钥均在 WSL 状态目录，未进入源码或构建包。

### 2.2 调度与两级账本

L4 在同一事务内更新请求、账户预留和全局预留，实际完成后结算两级消费。账户在途、窗口和预算各自生效，平台总容量独立约束全部用户。

模型池用平台私有密钥保护并再次校验固定容量。不同账号的相同 request_id 映射为独立内部身份；旧版请求保留原模型池凭据身份，以便升级后继续核对。

个人端查询 PlatformFeedback，依据账户队列、余额和窗口状态进行可取消等待。反馈只是发送协调依据，L4 继续执行权威准入。

### 2.3 界面与启动

个人端界面增加平台注册、登录、已有 Key 配置，以及账户余额、共享池容量/占用和本账户在途/排队。实际 task、resources、events 和 artifacts 接口继续提供状态。

统一 CLI 使用 platform、model-pool、client、executor、supervisor 等角色。停止独立个人端时核对监督进程的 PID、启动 ticks 和 boot_id，直接通知该进程清理子服务；工具仍按自有进程组回收。dev 默认启动两套个人端；client 和 stop-client 可独立管理新增 profile。所有产品角色仍以一个 Python distribution 发布。

### 2.4 正式文档

已重写项目说明书架构章节，移除个人端独占外部模型服务的产品路线，并同步 L3/L4 的额度与反馈表述。工程架构、搭建计划、根 README、AGENTS、配置示例和开发路线统一采用平台账号与固定模型池模型。

0.1 验收记录作为历史基线保留；PPT、旧说明与图稿继续保存历史设计，当前工程以本轮正式文档和代码为准。

## 3. 验证结果

### 3.1 自动化检查

| 验证组 | 结果 | 关键覆盖 |
|---|---|---|
| 静态检查。 | Ruff 格式/lint、mypy 的 36 个源文件、3 项导入约束通过。 | 配置隔离、类型和模块边界。 |
| 领域与集成。 | 29 项通过。 | 账户 Key 生命周期、权限隔离、账户/全局账本、同名请求隔离、反馈等待、旧账本核对、原有工具与取消。 |
| 完整进程。 | 4 项通过。 | 双账户真实工具任务、新 profile 注册与凭证恢复、SIGKILL 后重启、执行取消/续作及输入版本。 |
| Linux 专项。 | 4 项通过。 | 实际 OOM、daemon 重启、双账户 cgroup 回收、非进程组组长的前台监督进程停止。 |

自动化测试共 37 项。verify 构建 taskloom-0.2.0-py3-none-any.whl 与 sdist，并审计当前版本包。旧版 dist 保留，构建验证根据 pyproject.toml 精确选择当前 wheel。

### 3.2 独立安装

verify-isolated 在临时干净源码副本和新环境中执行 locked 安装、同一 verify、打包及包审计。随后从源码目录之外安装 wheel 和锁定运行依赖，执行版本查询与双账户 self-test。

结果为 0.2.0，两个客户端分别完成两文件、五词的任务。每个任务形成一个 Agent、三个 Step、三个 Attempt，具有两项前驱关系；预留结算为零。共享池容量为 2，本次观测峰值为 2。

原始日志见 [verify.log](../../agent-workspace/outputs/engineering/20261015-unified/verify.log)、[linux.log](../../agent-workspace/outputs/engineering/20261015-unified/linux.log) 和 [verify-isolated.log](../../agent-workspace/outputs/engineering/20261015-unified/verify-isolated.log)。

### 3.3 浏览器与实际升级

本机旧版开发服务中三个任务均已完成且执行静止，确认后停止旧版并升级启动。旧个人任务 ID 与成果状态全部保留。旧平台和模型凭据账本复制到新目录，原文件保留。

使用独立浏览器会话操作未登录 profile：注册 demo20261015，连接本平台账号，提交一个文件、五个词的资料任务并交付。页面读取真实账户与共享池数据，控制台和脚本错误为空。

验收新增的 enrollment-demo 个人端已使用 stop-client 结束，8799 端口已释放。Alice/Bob 的新版开发拓扑保留运行供查看。

1440 像素桌面截图已检查；390 × 844 移动视口的页面宽度为 390，无横向溢出。原始状态与截图见 [本轮输出目录](../../agent-workspace/outputs/engineering/20261015-unified/)。

## 4. 使用、升级与后续

### 4.1 使用入口

运行 `python tools/project.py dev`，打开 Alice 的 http://127.0.0.1:8795 与 Bob 的 http://127.0.0.1:8798。二者连接相同的 http://127.0.0.1:8796 平台。

在 dev 运行期间执行 `python tools/project.py client --profile charlie --port 8799`，可打开尚未配置的新个人端，在页面注册、登录或连接已有本平台 Key。使用 `stop-client --profile charlie` 停止该个人端。

dev 与客户端的 cgroup 委派均使用临时 systemd 用户 scope。本机环境为 Ubuntu 24.04.1 / WSL2 / Python 3.12.3，Windows 开发入口自动转交 WSL。

### 4.2 升级与回退

本轮修改前的产品源码、配置、当前说明书和工程文档备份在 [before-rewrite.zip](../../agent-workspace/outputs/engineering/20261015-unified/before-rewrite.zip)。原研究资料和历史文档保留。

首次升级从 gateway/state.db、provider/state.db 复制账本到 platform/state.db、platform/pool.db，新账本存在时保持原样。旧文件继续保留；新 profile 身份固定为 platform_id/account_id。回退前先停止相应进程，保留当前状态副本，再选择匹配版本的代码与账本。

### 4.3 后续开发

先扩展工作区与多工具、子 Agent 工作负载，再比较成本 DRR、阶段银行家、期限与老化；自适应反馈继续在当前账户反馈协议上增加。性能实验使用同一平台、同一账户配置和同一模型池，按完整任务评价新增收益及管理开销。
