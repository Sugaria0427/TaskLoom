# TaskLoom「织序」

由个人 Agent 客户端和统一模型服务平台组成的协同调度系统。

主仓库：[Gitee / Sugaria / TaskLoom](https://gitee.com/Sugaria/TaskLoom)。团队在 [Gitee Issues](https://gitee.com/Sugaria/TaskLoom/issues) 认领工作包、记录进展并提交 Pull Request；成员邀请和协作流程见 [Gitee 协作说明](docs/engineering/TaskLoom_Gitee协作说明.md)。[GitHub 仓库](https://github.com/Sugaria0427/TaskLoom)保留迁移前的历史副本。

用户在 TaskLoom 平台注册账号，个人端配置平台 API 地址及账号 API Key。本机执行 Agent 与工具，平台管理账户额度，并向所有用户提供同一个固定模型服务池。

## 1. 当前实现

0.2 已贯通平台注册、个人端连接、双用户任务、共享模型池和反馈等待。模型池使用固定容量仿真；文件工具、进程、cgroup、账户和持久账本实际运行。当前真实工具为 `file_stats`，默认模型池提供模拟决策与用量。成本 DRR、阶段银行家、子 Agent 扇出、自适应反馈与原版 OpenClaw 对照按后续阶段实施。已合并、待合并和研究设计集中记录在[当前实现与开发状态](docs/engineering/TaskLoom_当前实现与开发状态.md)。

| 部分 | 已实现能力 |
|---|---|
| 个人端 L1—L3。 | 有界 Agent 循环、根任务轮转、工具资源准入、任务预算、平台反馈等待、取消与续作。 |
| 平台端 L4。 | 注册/登录、Key 签发与撤销、用户/根任务轮转、账户与全局额度、有界队列、幂等结算。 |
| 固定模型池。 | 平台内部密钥、固定并发容量、实际在途/峰值记录、模拟结果与按请求查询。 |
| OS 与工程保障。 | Unix socket、子进程和读取线程、cgroup、独立守护、SQLite WAL、锁定依赖、类型与分层验证。 |

浏览器是个人端操作界面。本地 Python 服务推进 Agent 并调用工具；当前受管执行环境为 Linux/WSL2。

## 2. 本机启动

在项目根目录的 PowerShell 中执行：

```powershell
python tools/project.py doctor
python tools/project.py bootstrap
python tools/project.py dev
```

新成员可以先执行 `git clone https://gitee.com/Sugaria/TaskLoom.git`，进入克隆后的 TaskLoom 目录，再执行上述命令。

运行环境为 x86_64 Linux、Python 3.12；Windows 入口自动转交 Ubuntu WSL2。安装使用经 SHA-256 校验的项目内 uv 和 uv.lock。

开发拓扑启动固定模型池、统一平台，以及 Alice 和 Bob 两套隔离个人端。两名开发用户通过真实注册接口建立，各自获得平台 Key，使用相同平台和模型池。

| 地址 | 用途 |
|---|---|
| http://127.0.0.1:8795 | Alice 的个人端。 |
| http://127.0.0.1:8798 | Bob 的个人端。 |
| http://127.0.0.1:8796/api/v1/status | 平台公共容量与队列概览；/docs 查看接口。 |
| http://127.0.0.1:8797 | 平台内部固定模型池，推理与用量查询需要内部密钥。 |

个人端采用会话工作台：左侧管理会话，中央发送消息、添加 UTF-8 文本文件和查看成果，右侧查看执行步骤、资源预留与平台额度。“调度监控”展示逐任务状态、资源分配、消耗与排队趋势。会话支持搜索、重命名、归档和恢复，记录保存在本机数据库；每条消息启动独立的文档统计任务。

在两个个人端同时提交资料任务，可观察本地工具与共享模型服务。点击左下角平台账号可注册、登录或使用已有 API Key 连接；新账号使用新的 profile 保存任务。开发用户凭证和平台内部密钥保存在 WSL 私有状态目录。界面说明及验证见[个人端工作台与验收](docs/engineering/TaskLoom_个人端工作台与验收.md)。

体验新用户注册时，另开终端执行 `python tools/project.py client --profile charlie --port 8799`，打开 http://127.0.0.1:8799 配置本平台账号。结束该客户端使用 `python tools/project.py stop-client --profile charlie`。

Ctrl+C 结束前台运行，或另开终端执行 `python tools/project.py stop`。`dev --single-client` 只启动 Alice；`--port`、`--platform-port`、`--pool-port`、`--second-client-port` 覆盖端口。dev 使用临时 systemd 用户 scope 委派 cgroup，工具进入独立资源组。

## 3. 架构与配置

| 事实 | 权威入口 |
|---|---|
| 产品目标、统一账号模型与四层研究方案。 | [当前完整说明书](docs/TaskLoom_项目说明书_当前完整方案.md) |
| 实际进程、接口、账本与配置所有权。 | [基础工程架构](docs/engineering/TaskLoom_基础工程架构.md) |
| 工程约束与后续开发阶段。 | [AGENTS.md](AGENTS.md)、[工程搭建计划](docs/engineering/TaskLoom_工程骨架搭建计划.md)、[PR 交付约定](docs/engineering/TaskLoom_PR提交与验收约定.md) |
| 已合并能力、待合并增量与最新验证。 | [当前实现与开发状态](docs/engineering/TaskLoom_当前实现与开发状态.md) |
| L3 请求调度的组件边界与接线计划。 | [L3 调度组件接入方案](docs/engineering/TaskLoom_L3调度组件接入方案.md) |

个人端使用 ClientConfig，平台使用 PlatformConfig，固定模型池使用 PoolConfig。示例在 configs/dev；环境变量在 .env.example。个人端凭证文件为 clients/<profile>/platform-credentials.json，保存平台 URL 和本账号 Key；平台池密钥由平台持有。

.env.example 提供变量清单，实际设置来自 TOML 和 WSL 进程环境；服务不会自动读取 .env 文件。

模型请求始终经过本平台。`coordinated=false` 用于无反馈基线，保留同一鉴权、额度、模型池和调用路径。

[研究报告与来源索引](research/agent-orchestrate/README.md)与[正式文档](docs/README.md)分别维护证据和工程说明，旧版资料从[备份索引](backup/README.md)追溯。产品代码位于 src，产品工具位于 tools，文档编写工作区位于本地 agent-workspace，历史备份保存在 backup。

公开仓库包含源码、测试、配置模板、项目文档、调研报告与来源索引。原始论文、网页快照、编写工作区、运行数据和构建产物保留在本地，不纳入版本控制。来源索引提供公开链接，本地材料路径按原核查记录保留，供持有原始证据的成员追溯。

## 4. 验证与继续开发

```powershell
python tools/project.py check
python tools/project.py test
python tools/project.py test linux
python tools/project.py verify
python tools/project.py verify-isolated
```

check 检查格式、lint、严格类型和模块边界；test 包含领域、集成与完整进程流程。Linux 专项验证真实 OOM、守护重启和 cgroup 回收。verify 执行 locked 安装、静态检查、测试、构建与包审计；verify-isolated 从干净副本和新环境验证，再安装 wheel 到源码之外执行双账户 self-test。

当前基础轮转保留为算法对照。下一阶段先增加多工具和子 Agent 工作负载，再比较成本 DRR、阶段银行家、期限/老化与自适应反馈的真实增量。
