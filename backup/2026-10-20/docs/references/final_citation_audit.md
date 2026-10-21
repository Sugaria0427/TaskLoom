# TaskLoom最终主稿：29条引用与证据映射

最终稿处理记录：下述[3]耗时口径建议已于2026年9月29日纳入正文1.1；数字编号与分类编排说明也已更新。以下保留审查时的原始意见，引用映射继续有效。

核查对象：`docs/TaskLoom_开题项目计划书.md`。核查日期：2026-09-29。只复用本地归档并核对关键段落，未扩大检索、未修改主稿。下表保留主稿最终编号，不再使用选材表的selection_key充当编号。正式书目按类型编排，称“数字编号引用”即可，不宣称按首次出现排序。

## 1. 关键结论与必要纠正

**29条引用均有可追溯材料或元数据，未发现DOI错配、引用不存在、将社区fork当维护者发布或将遥测429当模型额度的问题。**主稿正确区分开题设计、文献效果和未独立复现的问题报告。

唯一需要主文精确化的是[3]：1.1“其特定样本的多数会话由模型耗时主导”容易包含用户空闲。原文4.2给出：单轮且包含模型和工具的session，LLM占比中位87.7%；多轮session墙钟中用户idle中位80.1%，其余执行部分LLM高于工具。建议改为“在其样本中，排除用户轮次间空闲后，模型推理耗时总体高于工具执行耗时；93%的工具批次只有一次调用”。93%及TraceLab的数量/4%–85%均可由归档正文支持。

[18]英文链接本轮HTTP跳转至JS壳，但已通过中文同篇官方页完整渲染取证，作者Marc Brooker可核；应以C04_browser.txt为证据，不以18字的C04.txt冒充全文。[25–26]仅核Crossref元数据及EDF摘要，主文只引算法思想并保留保证边界，使用适当。

## 2. 编号映射

### 课程与学术 [1–7]

**[1] 课程课件**

本地课件，无外网URL。材料：[操作系统原理课程设计(3).pdf](E:/Project/OS-Project/agent-workspace/inputs/course/操作系统原理课程设计(3).pdf)；[course_text.txt](E:/Project/OS-Project/agent-workspace/inputs/course/course_text.txt)

支撑范围：页3–6 OS机制、7–12守护、13–16进度/评分；文本第190–199行支持4–6人、第6周展示/第8周答辩。

限制与阅读：课程要求，不是项目已有实现。 归档原文相关段落和来源元数据复核；未独立运行

**[2] TraceLab: Characterizing Coding Agent Workloads for LLM Serving**

[原文](https://arxiv.org/abs/2606.30560v2)。材料：[P14_raw.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/P14/P14_raw.html)；[P15_text.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/P14/P15_text.txt)

支撑范围：真实coding agent traces包含长自主循环、重尾工具时长，可辅助构造负载分布而非凭空假设。

限制与阅读：无主机CPU/RAM轨迹，工具耗时含等待；去掉原始输入输出，不能直接重演语义或证明资源竞争；非课程实验数据。 正文相关段落已核；未独立复现实验

**[3] Agentic Coding in the Wild**

[原文](https://arxiv.org/abs/2608.00101)。材料：[P16_text.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/P16/P16_text.txt)；[P16_raw.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/P16/P16_raw.html)

支撑范围：工具批次93%单调用；单轮LLM占比中位87.7%，多轮需区分用户idle。

限制与阅读：原文多轮session墙钟idle中位80.1%；主文“多数会话由模型耗时主导”应补排除用户空闲的条件。 归档原文相关段落和来源元数据复核；未独立运行

**[4] AIOS: LLM Agent Operating System**

[原文](https://arxiv.org/abs/2403.16971v5)。材料：[P18_raw.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/P18/P18_raw.html)；[P18_text.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/P18/P18_text.txt)

支撑范围：已有Agent kernel/SDK、FIFO/RR及工具并发控制；TaskLoom不能宣称首次引入OS机制。

限制与阅读：arXiv元数据声明COLM，本轮未核正式会场页；效率条件含本地模型/高并发，不可把2.1倍当课设预期。 正文相关段落已核；未独立复现实验

**[5] An LLM Compiler for Parallel Function Calling**

[原文](https://proceedings.mlr.press/v235/kim24y.html)。材料：[llmcompiler_pmlr.txt](E:/Project/OS-Project/research/materials/web/2026-09-27/llmcompiler_pmlr.txt)；[llmcompiler.txt](E:/Project/OS-Project/research/materials/extracted/2026-09-27/llmcompiler.txt)

支撑范围：函数调用规划、任务获取与并行执行已有系统；依赖正确和可并行性属于既有基础。

限制与阅读：只据正式页摘要可支持三个组件和并行函数调用；资源有界、取消、配额联合控制不可从摘要推出；论文倍率不移植。 本次复核PMLR正式书目和摘要；旧库有全文提取，未在本次逐节复核

**[6] PeakBench: Benchmarking Resource-Aware Tool Invocation in LLM Agents**

[原文](https://arxiv.org/abs/2608.24509)。材料：[L07.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L1_local_resources/L07/L07.txt)；[L07.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L1_local_resources/L07/L07.html)

支撑范围：逻辑依赖正确不自动保证资源容量可行；可以分别评价依赖规划与容量约束调度；成熟规则必须作为基线。

限制与阅读：真实profiling+调度模拟；给定核验DAG，非普通PC现场；Best Rule与所提策略部分指标接近，CVA不等真实OOM。 正文相关段落已核；未独立复现实验

**[7] HiveMind: OS-Inspired Scheduling for Concurrent LLM Agent Workloads**

[原文](https://arxiv.org/abs/2604.17111)。材料：[A06.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L3_client_requests/A06/A06.txt)；[A07.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L3_client_requests/A07/A07.html)

支撑范围：客户端共享API已有准入、rate tracking、预算、priority/SJF/DAG和重试；须比较成熟组合。

限制与阅读：主要mock，重试主导；去准入一次消融仍全成功，本地direct/proxy皆无失败；不证明全新联合调度或工具资源收益。 正文相关段落已核；未独立复现实验

### 工作流与接口 [8–14]

**[8] Checkpointers**

[原文](https://docs.langchain.com/oss/python/langgraph/checkpointers)。材料：[P24_raw.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/P24/P24_raw.html)；[P24_text.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/P24/P24_text.txt)；[A10.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/A10/A10.txt)

支撑范围：checkpoint及pending writes已有，成功步骤可在规则下复用；失败不必一律整任务重跑。；persistence另存A10。

限制与阅读：不保存任意OS进程现场，不保证外部副作用exactly-once；依赖正确持久化与幂等边界。 正文相关段落已核；未独立复现实验

**[9] Task Queue Priority and Fairness**

[原文](https://docs.temporal.io/develop/task-queue-priority-fairness)；[原文](https://docs.temporal.io/guides/rate-limit-downstream-apis)。材料：[A24.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L4_platform_requests/A24/A24.html)；[A24.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L4_platform_requests/A24/A24.txt)；[A25.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L4_platform_requests/A25/A25.html)；[A25.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L4_platform_requests/A25/A25.txt)

支撑范围：工作流/Activity/子工作流优先继承与同级加权公平已有；可作任务治理基线。；下游全局Activity速率/并发/Retry-After见A25。

限制与阅读：优先保证分区内；fairness是派发比例不是token成本或整任务完成公平；配置/Cloud许可有条件。 正文相关段落已核；未独立复现实验

**[10] Dynamic TPM/RPM allocation**

[原文](https://docs.litellm.ai/docs/proxy/dynamic_rate_limit)；[原文](https://docs.litellm.ai/docs/proxy/io_token_rate_limits)。材料：[A13.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L4_platform_requests/A13/A13.html)；[A13.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L4_platform_requests/A13/A13.txt)；[A14.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L4_platform_requests/A14/A14.txt)

支撑范围：已有priority reservation、闲置借用；特定后计TPM路径可在并发下overshoot，适合定义强基线和限制。；Mantle式预留/结算路径另见A14。

限制与阅读：部分Beta/Enterprise；不能外推所有LiteLLM路径；不把钱预留与此token窗口当同一问题。 正文相关段落已核；未独立复现实验

**[11] LiteLLM PR38647**

[原文](https://github.com/BerriAI/litellm/pull/38647)。材料：[A21.json](E:/Project/OS-Project/research/agent-orchestrate/materials/L3_client_requests/A21/A21.json)；[A29.json](E:/Project/OS-Project/research/agent-orchestrate/materials/L3_client_requests/A29/A29.json)

支撑范围：最大费用预留可能保守；缩预留cap以可用性换瞬时超支风险。

限制与阅读：open、未合并、作者生产报告；不是项目复现或发布版功能证明。 归档原文相关段落和来源元数据复核；未独立运行

**[12] Function calling**

[原文](https://developers.openai.com/api/docs/guides/function-calling)。材料：[R02.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/R02/R02.html)；[R02.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/R02/R02.txt)

支撑范围：模型产生工具调用，应用执行工具并提交结果；说明调度控制点在runtime而不在模型文本本身。

限制与阅读：工具协议不提供完整任务DAG/未来耗时，不代表能任意暂停远端推理。 正文相关段落已核；未独立复现实验

**[13] OpenAI Conversation state**

[原文](https://developers.openai.com/api/docs/guides/conversation-state)。材料：[R01.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L4_platform_requests/R01/R01.html)；[R01.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L4_platform_requests/R01/R01.txt)

支撑范围：手动history与previous_response_id/Conversations等会话管理方式。

限制与阅读：不会自动产生完整任务DAG、截止时间或工具资源需求。 归档原文相关段落和来源元数据复核；未独立运行

**[14] Custom middleware**

[原文](https://docs.langchain.com/oss/python/langchain/middleware/custom)。材料：[R04.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/R04/R04.html)；[R04.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/R04/R04.txt)

支撑范围：wrap_model_call/wrap_tool_call提供模型与工具handler前后控制点，受控adapter接入有实际接口依据。

限制与阅读：只证明接口存在；不能假装透明控制所有闭源harness或据此推断整DAG自动可见。 正文相关段落已核；未独立复现实验

### 运行时和OS [15–20]

**[15] Tools subsystem**

[原文](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/tools.md)。材料：[R07.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L1_local_resources/R07/R07.html)；[R07.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L1_local_resources/R07/R07.txt)

支撑范围：已有tools/pre-execute、execute wrapper、result、signal、parallel/exclusive及协作quiescence契约；适配器不能忽略既有控制。

限制与阅读：master可变，本地snapshot可核；静态文档不等全链已运行验证；取消信号不自动终止不合作外部资源。 正文相关段落已核；未独立复现实验

**[16] Subagent subsystem**

[原文](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/subagent.md)。材料：[source_subagent_docs.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/DS_subagent_docs/source_subagent_docs.txt)；[source_checks.json](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/DS_repo/source_checks.json)

支撑范围：子agent生命周期、所有权、启动/续接契约是已有runtime能力；可据此划分TaskLoom职责。

限制与阅读：本次选读静态master文档，未证发布版本或所有异常路径正确。 正文相关段落已核；未独立复现实验

**[17] Control Group v2**

[原文](https://docs.kernel.org/admin-guide/cgroup-v2.html)。材料：[C03.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L1_local_resources/C03/C03.html)；[C03.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L1_local_resources/C03/C03.txt)

支撑范围：cpu.max/weight、memory.high/max、pids.max与资源组管理提供真实资源隔离控制。

限制与阅读：memory.high回收可能变慢，memory.max可能OOM；限制不是成功保证；要纳入真实后代并预留监督器资源。 正文相关段落已核；未独立复现实验

**[18] Marc Brooker: Timeouts, retries, and backoff with jitter**

[原文](https://aws.amazon.com/cn/builders-library/timeouts-retries-and-backoff-with-jitter/)。材料：[C04_browser.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L3_client_requests/C04/C04_browser.txt)；[C04_browser_metadata.json](E:/Project/OS-Project/research/agent-orchestrate/materials/L3_client_requests/C04/C04_browser_metadata.json)

支撑范围：多层重试放大、超时不代表无副作用、退避/jitter及有界尝试。

限制与阅读：实际阅读的是浏览器渲染中文官方页；C04.txt仅JS壳不是有效正文。跨五层3次的243是示意而非Agent事故统计。 归档原文相关段落和来源元数据复核；未独立运行

**[19] DeepSeek Discussion7936**

[原文](https://github.com/deepseek-ai/deepseek-harness/discussions/7936)。材料：[D7936.json](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/D7936/D7936.json)；[source_continuation.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/DS_continuation/source_continuation.txt)；[source_activation.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/DS_activation/source_activation.txt)

支撑范围：失败消息留下所有权依赖，关联静态代码支持顺序/回滚问题。

限制与阅读：社区复现/源码静态核验；非团队运行验证，master不等已发布版本。 归档原文相关段落和来源元数据复核；未独立运行

**[20] Codex Issue48524**

[原文](https://github.com/openai/codex/issues/48524)。材料：[48524.json](E:/Project/OS-Project/research/agent-orchestrate/materials/L1_local_resources/CX48524/48524.json)；[findings.md](E:/Project/OS-Project/research/agent-orchestrate/reports/02_solutions_and_integration.md)

支撑范围：app-server EOF清理fixture与其它exec路径清理成功的对照。

限制与阅读：用户报告和版本特定静态检查，不宣称所有MCP清理均失败。 归档原文相关段落和来源元数据复核；未独立运行

### 补充材料、算法与provider [21–29]

**[21] Claude Issue70498**

[原文](https://github.com/anthropics/claude-code/issues/70498)。材料：[S02.json](E:/Project/OS-Project/research/agent-orchestrate/materials/_archive/documentation_inputs/supplemental_report/verification/S02.json)；[S05.json](E:/Project/OS-Project/research/agent-orchestrate/materials/_archive/documentation_inputs/supplemental_report/verification/S05.json)；[assessment.md](E:/Project/OS-Project/research/agent-orchestrate/materials/_archive/documentation_inputs/supplemental_report/assessment.md)

支撑范围：高输入量扇出、API错误终止及产物完成状态可能混淆。

限制与阅读：ITPM根因为作者推断；duplicate→64177后stale非已修；不能合并所有429/500/529原因。 归档原文相关段落和来源元数据复核；未独立运行

**[22] Claude Issue30378**

[原文](https://github.com/anthropics/claude-code/issues/30378)。材料：[S03.json](E:/Project/OS-Project/research/agent-orchestrate/materials/_archive/documentation_inputs/supplemental_report/verification/S03.json)；[S06.json](E:/Project/OS-Project/research/agent-orchestrate/materials/_archive/documentation_inputs/supplemental_report/verification/S06.json)；[assessment.md](E:/Project/OS-Project/research/agent-orchestrate/materials/_archive/documentation_inputs/supplemental_report/assessment.md)

支撑范围：历史用户报告OTEL export重试拖延退出，支持辅助收尾有界化。

限制与阅读：429属于遥测不是模型服务；约300–400MB等是用户报告；不证明当前版本仍受影响。 归档原文相关段落和来源元数据复核；未独立运行

**[23] Codex Issue22779**

[原文](https://github.com/openai/codex/issues/22779)。材料：[S01.json](E:/Project/OS-Project/research/agent-orchestrate/materials/_archive/documentation_inputs/supplemental_report/verification/S01.json)；[assessment.md](E:/Project/OS-Project/research/agent-orchestrate/materials/_archive/documentation_inputs/supplemental_report/assessment.md)

支撑范围：完成/驻留/活跃计数需区分；作者报告spawn仍被拒。

限制与阅读：fork为issue作者而非维护者修复；未核上游合并，不据此否定当前resident eviction。 归档原文相关段落和来源元数据复核；未独立运行

**[24] 用户补充报告**

本地附件，无外网URL。材料：[original_report.pdf](E:/Project/OS-Project/research/agent-orchestrate/materials/_archive/documentation_inputs/supplemental_report/original_report.pdf)；[fulltext.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/_archive/documentation_inputs/supplemental_report/fulltext.txt)；[assessment.md](E:/Project/OS-Project/research/agent-orchestrate/materials/_archive/documentation_inputs/supplemental_report/assessment.md)

支撑范围：只证明材料提出了哪些线索和设计建议；原案例按21–23回原始记录引用。

限制与阅读：二手材料不能独立证明集中调度不存在、性能损失或当前修复状态。 归档原文相关段落和来源元数据复核；未独立运行

**[25] Efficient fair queuing using deficit round-robin**

[原文](https://doi.org/10.1109/90.502236)。材料：[references_verified.json](E:/Project/OS-Project/docs/references/references_verified.json)

支撑范围：Deficit Round-Robin的经典算法出处；开题拟借用欠额累计思想，以服务成本而非单纯请求次数安排公平轮转。

限制与阅读：原工作面向网络包公平队列；本次核书目未阅读全文，不声称其复杂度/延迟界在未知成本、非抢占Agent中自动成立。 Crossref书目及所含摘要（DRR记录无摘要）；未阅读全文、未独立复现

**[26] Scheduling Algorithms for Multiprogramming in a Hard-Real-Time Environment**

[原文](https://doi.org/10.1145/321738.321743)。材料：[references_verified.json](E:/Project/OS-Project/docs/references/references_verified.json)

支撑范围：动态按当前deadline赋优先级的经典单处理器实时调度依据；开题可用EDF作明确截止时限任务的对照。

限制与阅读：摘要研究单处理器实时模型；未知执行时长、外部不可抢占API、依赖DAG与多资源不满足原定理前提，不能宣称Agent deadline全部保证。 Crossref书目及所含摘要（DRR记录无摘要）；未阅读全文、未独立复现

**[27] Rate limits**

[原文](https://developers.openai.com/api/docs/guides/rate-limits)。材料：[A01.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L3_client_requests/A01/A01.html)；[A01.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L3_client_requests/A01/A01.txt)

支撑范围：组织/project/模型共享速率域、RPM/TPM等限制与Retry-After；失败和多层重试需正确计账。

限制与阅读：动态文档；各模型合同不同，额度上限不是保证吞吐；客户端不能由此取得GPU调度权。 正文相关段落已核；未独立复现实验

**[28] Rate limits**

[原文](https://platform.claude.com/docs/en/api/rate-limits)。材料：[A02.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L3_client_requests/A02/A02.html)；[A02.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L3_client_requests/A02/A02.txt)

支撑范围：请求/输入输出tokens分开；token bucket随时间补充；当前OTPM按实际输出，max_tokens不直接计OTPM。

限制与阅读：须固定模型/版本；不能套用其他provider预留规则；订阅五小时额度不是此API合同。 正文相关段落已核；未独立复现实验

**[29] Job Objects**

[原文](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。材料：[C11.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L1_local_resources/C11/C11.html)；[C11.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L1_local_resources/C11/C11.txt)

支撑范围：Windows Job Objects可管理进程组资源与kill-on-close等生命周期。

限制与阅读：Windows与Linux接口不等价；嵌套、继承、breakaway与权限必须按实际版本验证。 正文相关段落已核；未独立复现实验

## 3. 主稿中的组合引用与边界

[8]合并persistence/checkpointers，[9]合并Temporal priority/fairness和downstream rate limiting，[10]合并动态份额与ITPM/OTPM，两页面不可彼此替代；本映射已分别给材料。[14–18]支撑运行时拦截、OS控制与重试等不同能力，是组合论据，不表示每个来源都包含整套系统。[21–24]只用于附件线索复核，不能合计为四份独立实证。

[11]是未合并提案，不能拿来证明所提成本cap已发布；官方现有预算预留还可查选材表LITELLM_BUDGET/A05。主稿此处仅要求分别核验计账路径和比较预留权衡，没有越过证据。文献倍率没有被写成项目预期；主稿10%工程目标已明确自选且拟验证。

本次所有映射材料路径存在。引用编号1—29无断号；后续版面改动不应改动原文ID或案例归因。issue状态仅以归档日期为准，最终实现前仍需重测目标版本。
