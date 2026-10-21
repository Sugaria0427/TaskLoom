# TaskLoom 工程约束

## 1. 范围与入口

项目范围以 docs/TaskLoom_项目说明书_当前完整方案.md 为准。实施步骤见
docs/engineering/TaskLoom_工程骨架搭建计划.md，实际能力及开发状态见 docs/engineering/TaskLoom_当前实现与开发状态.md，启动命令见根 README。
首版在 WSL2 Ubuntu / Python 3.12 运行；Windows 上的 tools/project.py 将操作转交给 WSL。
个人端 L1—L3 与平台 L4 调度模型推理以外的执行。OpenClaw 是独立对照与后续接入对象。
两端是同一 TaskLoom 系统：个人端使用平台注册账号的 API Key，所有模型请求经平台 L4。
平台持有固定模型服务池。反馈开关只切换算法，保持平台调用路径不变。

## 2. 代码与所有权

- domain/policies 使用标准库。client/gateway/runtime 依赖端口，bootstrap 装配实际适配器。
- 用户拥有根任务；Agent、Step、Attempt 继承归属。根任务扇出不增加份额。
- 每个 profile 的任务、资源和私有预算由 client 管理；平台账号及账户/全局账本由单进程平台服务管理。
- ClientConfig、PlatformConfig、PoolConfig 分别拥有各端配置；个人端不持有模型池私钥。
- profile 固定绑定 platform_id/account_id；账户的多个 API Key 共用该账号额度。
- 数据库、输入、产物和日志位于配置的状态目录；包内静态文件使用 importlib.resources。

## 3. 正确性规则

- 准入与预留原子更新；取消意图先保存；实际停止、成果发布和费用结算独立记录。
- 重试建立新 Attempt。重复传输复用 request_id；旧结果不覆盖当前结果，实际消费结算一次。
- 已派发但结果未知的工作进入核对；保留相应预留，禁止未经核对自动重放。
- 队列、正文、输出和产物设上限。长网络/工具工作位于锁和数据库事务之外。

## 4. 开发与验证

- 新依赖登记 pyproject.toml 并更新 uv.lock；常规安装使用 locked 模式。
- Python 公共接口完整标注类型，外部 JSON 使用 contracts 校验；异常转换为稳定错误码。
- 统一入口为 python3 tools/project.py。改动运行定向 test；里程碑运行 verify。
- HTTP/IPC/存储改动运行 integration；进程/cgroup/守护改动运行 test linux；交付运行 verify-isolated。

README 记录可复制命令，工具驱动定义实际检查。修改契约同步调用方与测试，修改状态/额度同步不变量。
文档与研究编写工具继续放 agent-workspace，产品开发工具放 tools。
保留现有资料和用户修改；本地建仓不自动 commit、添加 remote、push 或发布。

## 5. 变更与 PR 交付

- **范围与接线。** PR 关联工作包，说明本次独立验收范围、实际调用入口和跨组接口依赖。基础组件、已接入能力和后续计划分别标明；能力说明与运行路径一致。
- **契约衔接。** 跨模块改动同步生产者、消费者与测试，核对名称/参数、数量/单位、身份/版本、状态/终态。未知能力明确拒绝；等待、过期、取消和异常均有可核对的归属与结束路径。
- **行为验证。** 按需求不变量设计测试。调度覆盖成本份额、可用并发利用、持续竞争下的活性和失败收敛；跨模块至少覆盖一条真实消费边界。测试替身采用现有契约及真实状态语义。
- **提交证据。** PR 记录验证所对应的 head、环境、命令和结果，区分通过、失败与未执行。修订逐项关联审查意见、回归测试与最新验证；累计或替代 PR 标明继承关系并承接未解决意见。
