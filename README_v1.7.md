# TaskLoom「织序」

文档版本：v1.7 · 更新日期：2026-10-28。

由个人 Agent 客户端和统一模型服务平台组成的协同调度系统。

主仓库：[Gitee / Sugaria / TaskLoom](https://gitee.com/Sugaria/TaskLoom)。团队在 [Gitee Issues](https://gitee.com/Sugaria/TaskLoom/issues) 认领工作包、记录进展并提交 Pull Request；成员邀请和协作流程见 [Gitee 协作说明](docs/engineering/TaskLoom_Gitee协作说明.md)。[GitHub 仓库](https://github.com/Sugaria0427/TaskLoom)保留迁移前的历史副本。

用户在 TaskLoom 平台注册账号，个人端配置平台 API 地址及账号 API Key。本机执行 Agent 与工具，平台管理账户额度，并向所有用户提供同一个固定模型服务池。

## 1. 当前实现

0.2 已贯通平台注册、个人端连接、双用户任务、共享模型池和反馈等待。默认模型池使用固定容量仿真，也可配置真实 DeepSeek HTTP 上游；文件工具、进程、cgroup、账户和持久账本实际运行。个人端支持普通多轮对话、文本与图片附件、指定路径读取和逐条确认的本地命令执行。真实工具为 `read_file`、`run_command`、`file_stats`、`grep`、`list_files`、`top_words`，默认模型池提供模拟决策与用量。个人端已接入成本 DRR、期限/老化与反馈准入，平台已接入可配置的用户—根任务调度及硬预算保护，支持调度控制台和策略热切换。个人端提供三策略配对仿真页面，保存和导出长期对比结果。受控并行子 Agent 已实现：非对话任务可声明至多 8 个有界分支，分支共享根任务预算与服务份额，支持依赖汇合、必需/可选失败策略与取消/续作恢复，见[受控并行子 Agent 与依赖汇合](docs/engineering/TaskLoom_受控并行子Agent与依赖汇合.md)。阶段银行家、自适应反馈与原版 OpenClaw 对照继续按后续阶段实施。已合并、待合并和研究设计集中记录在[当前实现与开发状态](docs/engineering/TaskLoom_当前实现与开发状态.md)。

| 部分 | 已实现能力 |
|---|---|
| 个人端 L1—L3。 | 有界 Agent 循环、成本 DRR、期限/老化、工具准入、费用与 Token 分离判断、取消与续作；受控并行子 Agent（依赖汇合、必需分支快速失败、未核对分支阻止汇总）。 |
| 平台端 L4。 | 注册/登录、Key 签发与撤销、用户/根任务可配置调度、费用及 Token 硬上界、有界队列、旧账本恢复与幂等结算。 |
| 固定模型池。 | 平台内部密钥、固定并发容量、实际在途/峰值记录、模拟结果与按请求查询；活动请求锁及时回收，历史幂等回执保存在 SQLite。 |
| OS 与工程保障。 | Unix socket、子进程和读取线程、cgroup、独立守护、SQLite WAL、锁定依赖、类型与分层验证。 |

浏览器是个人端操作界面。本地 Python 服务推进 Agent 并调用工具；受管执行环境为 x86_64 Linux，可使用 Ubuntu 虚拟机、VS Code Remote-SSH 或 WSL2。

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
| http://127.0.0.1:8796/admin | 平台调度控制台：按账号和根任务查看占槽、排队原因、账本与事件，热切换调度策略；需要平台管理员密钥：开发拓扑生成后保存在状态目录的 `platform/platform.toml`（字段 `admin_key`），dev 启动输出的 `admin_key_file` 给出完整路径。 |
| http://127.0.0.1:8797 | 平台内部固定模型池，推理与用量查询需要内部密钥。 |

个人端采用会话工作台：左侧管理会话，中央发送消息、添加 UTF-8 文本与图片、选择技能和查看回复，右侧查看执行步骤、资源预留与平台额度。“调度监控”展示逐任务状态、资源分配、消耗与排队趋势。会话支持搜索、重命名、归档和恢复，记录保存在本机数据库；每条消息建立受控根任务，携带最近八轮成功对话。同一会话顺序推进，不同会话可并行执行。

在两个个人端同时提交资料任务，可观察本地工具与共享模型服务。点击左下角平台账号可注册、登录或使用已有 API Key 连接；新账号使用新的 profile 保存任务。开发用户凭证和平台内部密钥保存在 WSL 私有状态目录。界面说明及验证见[个人端工作台与验收](docs/engineering/TaskLoom_个人端工作台与验收.md)。

体验新用户注册时，另开终端执行 `python tools/project.py client --profile charlie --port 8799`，打开 http://127.0.0.1:8799 配置本平台账号。结束该客户端使用 `python tools/project.py stop-client --profile charlie`。

真实模型启动前，把 `configs/dev/upstream.example.json` 复制到仓库外并填写私有密钥；真实调用会使用上游账户额度。当前机器已经配置的启动命令为：

```powershell
python tools/project.py dev --scenario api_constrained --upstream-config /home/sugaria/.config/taskloom/deepseek-private.json
```

所有请求继续经过平台 L4；上游密钥由平台持有。真实模型与模拟模型使用独立的状态目录和账本。停止时使用 `python tools/project.py stop --upstream-config /home/sugaria/.config/taskloom/deepseek-private.json`。真实用量显示为 Token 单位；货币费用以供应商账单为准。

从个人端侧栏打开“对照实验”（http://127.0.0.1:8795/experiments），选择 1/7/30 天、用户数、资源容量与随机种子，即可比较 FIFO、平台轮转与 TaskLoom 组合策略。该页使用合成负载并复用生产策略，不调用付费 API；结果、逐日曲线、波动与事件样本均可导出。执行监控另提供真实记录导出。完整操作及模型边界见[课程验收与演示](docs/engineering/TaskLoom_课程验收与演示_v1.0.md)。

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

平台配置 `scheduling_policy` 默认仍是 `rr`（用户/根任务轮转）。候选策略 `hybrid` 的规则是：空槽先给当前占槽最少的用户、用户内部再给占槽最少的根任务，占槽相同时近期槽位秒少的优先；全量配对确认后再决定是否改为默认。`drr`、`maxmin` 保留为对照，实验数据见 [WP5 调度策略设计](docs/engineering/TaskLoom_WP5_L4调度策略设计.md)。运行中可在平台调度控制台（`/admin`，接口 `GET /admin/v1/scheduler`、`POST /admin/v1/scheduler/settings`）切换策略和 `cache_wait_seconds`：排队请求按原顺序迁移到新策略，修改持久化并留审计事件；`admin_key` 为空时管理接口关闭。不启动完整拓扑也能演示：`uv run --locked python tests/l4/console_demo.py` 在任意系统上启动平台和回放模型池。有本地 Aether 轨迹时回放真实负载，没有时自动改用合成负载，见 [L4 开发环境 §5](tests/l4/README_v1.1.md#5-调度控制台演示)。`pool_accounts` 列出多个上游账号时，网关在派发时按缓存亲和选择账号，留空则与单账号配置相同。下一阶段先增加混合工具和子 Agent 工作负载，再比较成本 DRR、阶段银行家、期限/老化与自适应反馈的真实增量。


本地优先级已接入模型成本公平调度与工具加权队列，运行中的执行保持原资源。WP4 保存每次工具执行的资源声明与回执，重启后查询原身份。Agent 和执行器按工具名称、参数模式和资源画像准入。普通对话直接返回自然语言；输入 `/` 可选择中文「笔记整理」「资料统计」技能，仅本轮注入提示词，统计技能使用 file_stats 回执核验数字。用户消息直接保存为对话，不生成 notes.txt 或 summary.json；旧统计任务保留原成果与恢复协议。

WP6 提供固定负载、持续保存的实验清单、快照、质量指标、实测工作流校准与 FIFO 容量仿真，入口见[实验指标与复现](docs/engineering/WP6_实验指标与复现.md)。原 FIFO 容量仿真保持原用途；新增界面对照使用三种策略和固定合成工作流。五组真实消融仍按研究计划推进。


本版汇集 WP2 多工具与恢复、WP4 本地资源、WP6 实验数据链，以及个人端和平台端请求调度。Linux/Ubuntu 终端使用 `python3.12 tools/project.py bootstrap` 和 `python3.12 tools/project.py dev`；远程虚拟机可通过 VS Code 端口转发访问个人端。Windows 启动入口继续委派 Ubuntu WSL2。


本地工具与技能的操作步骤、权限范围、资源口径及验收方法见[个人端工作台](docs/engineering/TaskLoom_个人端工作台与验收.md)。平台设置中可查看通用中文提示词与当前工作目录。
