# TaskLoom「织序」开题计划书：引用选材核验表

本表选取26条学术、官方及少量问题单资料。`selection_key`只作稳定映射，**不代表最终参考文献编号**；编写者按正文首次出现顺序生成数字引用。详细元数据及绝对本地材料路径见[references_verified.json](references_verified.json)。动态文档未核出版年时留空，不能把2026访问年伪装成发表年。

## 1. 学术先例与负载依据

论文用于证明已有方法、合理研究对象和评价边界。TaskLoom尚是proposal，所有结果应写“拟验证/计划比较”，论文倍率不得作为预期改进、验收承诺或已完成结果。AIOS的COLM2025信息来自arXiv自报；LLMCompiler的ICML/PMLR正式书目已核。其余三篇按预印本引用。

### 学术条目

**AIOS — AIOS: LLM Agent Operating System**

- 书目：Kai Mei, Xi Zhu, Wujiang Xu, Wenyue Hua, Mingyu Jin, Zelong Li, Shuyuan Xu, Ruosong Ye, Yingqiang Ge, Yongfeng Zhang；2025；arXiv v5；arXiv comments声明发表于COLM 2025；[原始页面](https://arxiv.org/abs/2403.16971v5)。
- 可支持：已有Agent kernel/SDK、FIFO/RR及工具并发控制；TaskLoom不能宣称首次引入OS机制。
- 限制：arXiv元数据声明COLM，本轮未核正式会场页；效率条件含本地模型/高并发，不可把2.1倍当课设预期。
- 阅读与材料：正文相关段落已核；未独立复现实验；[P18_text.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/P18/P18_text.txt)。

**LLMCOMPILER — An LLM Compiler for Parallel Function Calling**

- 书目：Sehoon Kim, Suhong Moon, Ryan Tabrizi, Nicholas Lee, Michael W. Mahoney, Kurt Keutzer, Amir Gholami；2024；ICML 2024; PMLR 235:24370–24391；[原始页面](https://proceedings.mlr.press/v235/kim24y.html)。
- 可支持：函数调用规划、任务获取与并行执行已有系统；依赖正确和可并行性属于既有基础。
- 限制：只据正式页摘要可支持三个组件和并行函数调用；资源有界、取消、配额联合控制不可从摘要推出；论文倍率不移植。
- 阅读与材料：本次复核PMLR正式书目和摘要；旧库有全文提取，未在本次逐节复核；[llmcompiler.txt](E:/Project/OS-Project/research/materials/extracted/2026-09-27/llmcompiler.txt)。

**PEAKBENCH — PeakBench: Benchmarking Resource-Aware Tool Invocation in LLM Agents**

- 书目：Zhi-Kai Chen, Xu-Xiang Zhong, Song-Yan Li, De-Chuan Zhan, Han-Jia Ye；2026；arXiv预印本；[原始页面](https://arxiv.org/abs/2608.24509)。
- 可支持：逻辑依赖正确不自动保证资源容量可行；可以分别评价依赖规划与容量约束调度；成熟规则必须作为基线。
- 限制：真实profiling+调度模拟；给定核验DAG，非普通PC现场；Best Rule与所提策略部分指标接近，CVA不等真实OOM。
- 阅读与材料：正文相关段落已核；未独立复现实验；[L07.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L1_local_resources/L07/L07.html)。

**HIVEMIND — HiveMind: OS-Inspired Scheduling for Concurrent LLM Agent Workloads**

- 书目：Justice Owusu Agyemang, Jerry John Kponyo, Obed Kwasi Somuah, Elliot Amponsah, Godfred Manu Addo Boakye, Kwame Opuni-Boachie Obour Agyekum；2026；arXiv预印本；[原始页面](https://arxiv.org/abs/2604.17111)。
- 可支持：客户端共享API已有准入、rate tracking、预算、priority/SJF/DAG和重试；须比较成熟组合。
- 限制：主要mock，重试主导；去准入一次消融仍全成功，本地direct/proxy皆无失败；不证明全新联合调度或工具资源收益。
- 阅读与材料：正文相关段落已核；未独立复现实验；[A07.html](E:/Project/OS-Project/research/agent-orchestrate/materials/L3_client_requests/A07/A07.html)。

**TRACELAB — TraceLab: Characterizing Coding Agent Workloads for LLM Serving**

- 书目：Kan Zhu, Mathew Jacob, Chenxi Ma, Yi Pan, Stephanie Wang, Arvind Krishnamurthy, Baris Kasikci；2026；arXiv预印本；[原始页面](https://arxiv.org/abs/2606.30560v2)。
- 可支持：真实coding agent traces包含长自主循环、重尾工具时长，可辅助构造负载分布而非凭空假设。
- 限制：无主机CPU/RAM轨迹，工具耗时含等待；去掉原始输入输出，不能直接重演语义或证明资源竞争；非课程实验数据。
- 阅读与材料：正文相关段落已核；未独立复现实验；[P15_text.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/P14/P15_text.txt)。

### 官方能力、接入契约与资源机制

**OPENAI_QUOTA — Rate limits**

- 书目：OpenAI；出版年未核；OpenAI documentation；[原始页面](https://developers.openai.com/api/docs/guides/rate-limits)。
- 可支持：组织/project/模型共享速率域、RPM/TPM等限制与Retry-After；失败和多层重试需正确计账。
- 限制：动态文档；各模型合同不同，额度上限不是保证吞吐；客户端不能由此取得GPU调度权。
- 阅读与材料：正文相关段落已核；未独立复现实验；[A01.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L3_client_requests/A01/A01.txt)。

**CLAUDE_QUOTA — Rate limits**

- 书目：Anthropic；出版年未核；Claude API documentation；[原始页面](https://platform.claude.com/docs/en/api/rate-limits)。
- 可支持：请求/输入输出tokens分开；token bucket随时间补充；当前OTPM按实际输出，max_tokens不直接计OTPM。
- 限制：须固定模型/版本；不能套用其他provider预留规则；订阅五小时额度不是此API合同。
- 阅读与材料：正文相关段落已核；未独立复现实验；[A02.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L3_client_requests/A02/A02.txt)。

**LITELLM_BUDGET — Budget Setup Options**

- 书目：LiteLLM project；出版年未核；LiteLLM documentation；[原始页面](https://docs.litellm.ai/docs/proxy/users)。
- 可支持：已有个人/team/agent预算和最坏成本预留→实际结算；不能称网关都只事后统计。
- 限制：DB依赖、路径/版本差异；文档不等性能评测，预算作用域和在途残值仍需明确。
- 阅读与材料：正文相关段落已核；未独立复现实验；[A05.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L4_platform_requests/A05/A05.txt)。

**LITELLM_DYNAMIC — Dynamic TPM/RPM allocation**

- 书目：LiteLLM project；出版年未核；LiteLLM documentation；[原始页面](https://docs.litellm.ai/docs/proxy/dynamic_rate_limit)。
- 可支持：已有priority reservation、闲置借用；特定后计TPM路径可在并发下overshoot，适合定义强基线和限制。
- 限制：部分Beta/Enterprise；不能外推所有LiteLLM路径；不把钱预留与此token窗口当同一问题。
- 阅读与材料：正文相关段落已核；未独立复现实验；[A13.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L4_platform_requests/A13/A13.txt)。

**TEMPORAL_FAIRNESS — Task Queue Priority and Fairness**

- 书目：Temporal project；出版年未核；Temporal documentation；[原始页面](https://docs.temporal.io/develop/task-queue-priority-fairness)。
- 可支持：工作流/Activity/子工作流优先继承与同级加权公平已有；可作任务治理基线。
- 限制：优先保证分区内；fairness是派发比例不是token成本或整任务完成公平；配置/Cloud许可有条件。
- 阅读与材料：正文相关段落已核；未独立复现实验；[A24.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L4_platform_requests/A24/A24.txt)。

**TEMPORAL_RATE — Rate limit downstream APIs**

- 书目：Cecil Phillip；出版年未核；Temporal documentation；[原始页面](https://docs.temporal.io/guides/rate-limit-downstream-apis)。
- 可支持：可分API队列、全局Activity RPS、worker并发、Retry-After与重试策略，均非新功能。
- 限制：Activity次数不等token；一个activity多次调用需额外记账；教程举例额度不是当前真实套餐。
- 阅读与材料：正文相关段落已核；未独立复现实验；[A25.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L4_platform_requests/A25/A25.txt)。

**LANGGRAPH_CHECKPOINT — Checkpointers**

- 书目：LangChain project；出版年未核；LangGraph documentation；[原始页面](https://docs.langchain.com/oss/python/langgraph/checkpointers)。
- 可支持：checkpoint及pending writes已有，成功步骤可在规则下复用；失败不必一律整任务重跑。
- 限制：不保存任意OS进程现场，不保证外部副作用exactly-once；依赖正确持久化与幂等边界。
- 阅读与材料：正文相关段落已核；未独立复现实验；[P24_text.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/P24/P24_text.txt)。

**LANGCHAIN_INTERCEPT — Custom middleware**

- 书目：LangChain project；出版年未核；LangChain documentation；[原始页面](https://docs.langchain.com/oss/python/langchain/middleware/custom)。
- 可支持：wrap_model_call/wrap_tool_call提供模型与工具handler前后控制点，受控adapter接入有实际接口依据。
- 限制：只证明接口存在；不能假装透明控制所有闭源harness或据此推断整DAG自动可见。
- 阅读与材料：正文相关段落已核；未独立复现实验；[R04.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/R04/R04.txt)。

**LANGCHAIN_RATE — Models — Rate limiting**

- 书目：LangChain project；出版年未核；LangChain documentation；[原始页面](https://docs.langchain.com/oss/python/langchain/models)。
- 可支持：InMemoryRateLimiter线程安全且同进程可共享，但只限制请求次数、不限制请求大小。
- 限制：是该内存限流器的边界，不是整个LangChain没有token治理集成。
- 阅读与材料：正文相关段落已核；未独立复现实验；[A27.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L3_client_requests/A27/A27.txt)。

**DEEPSEEK_TOOLS — Tools subsystem**

- 书目：DeepSeek Harness project；出版年未核；DeepSeek Harness repository documentation；[原始页面](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/tools.md)。
- 可支持：已有tools/pre-execute、execute wrapper、result、signal、parallel/exclusive及协作quiescence契约；适配器不能忽略既有控制。
- 限制：master可变，本地snapshot可核；静态文档不等全链已运行验证；取消信号不自动终止不合作外部资源。
- 阅读与材料：正文相关段落已核；未独立复现实验；[R07.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L1_local_resources/R07/R07.txt)。

**DEEPSEEK_SUBAGENT — Subagent subsystem**

- 书目：DeepSeek Harness project；出版年未核；DeepSeek Harness repository documentation；[原始页面](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/subagent.md)。
- 可支持：子agent生命周期、所有权、启动/续接契约是已有runtime能力；可据此划分TaskLoom职责。
- 限制：本次选读静态master文档，未证发布版本或所有异常路径正确。
- 阅读与材料：正文相关段落已核；未独立复现实验；[source_checks.json](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/DS_repo/source_checks.json)。

**DEEPSEEK_LLM — LLM streaming subsystem**

- 书目：DeepSeek Harness project；出版年未核；DeepSeek Harness repository documentation；[原始页面](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/llm-streaming.md)。
- 可支持：LLM adapter/stream包装、重试和取消链可提供客户端配额接入点。
- 限制：不能把结束可见等待当真实socket已取消；费用未知必须保留未知状态；master是快照。
- 阅读与材料：正文相关段落已核；未独立复现实验；[R06.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L3_client_requests/R06/R06.txt)。

**CGROUP — Control Group v2**

- 书目：The Linux Kernel project；出版年未核；The Linux Kernel documentation；[原始页面](https://docs.kernel.org/admin-guide/cgroup-v2.html)。
- 可支持：cpu.max/weight、memory.high/max、pids.max与资源组管理提供真实资源隔离控制。
- 限制：memory.high回收可能变慢，memory.max可能OOM；限制不是成功保证；要纳入真实后代并预留监督器资源。
- 阅读与材料：正文相关段落已核；未独立复现实验；[C03.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L1_local_resources/C03/C03.txt)。

**JOB_OBJECTS — Job Objects**

- 书目：Microsoft；出版年未核；Microsoft Learn；[原始页面](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。
- 可支持：Windows Job Objects可管理进程组资源与kill-on-close等生命周期。
- 限制：Windows与Linux接口不等价；嵌套、继承、breakaway与权限必须按实际版本验证。
- 阅读与材料：正文相关段落已核；未独立复现实验；[C11.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L1_local_resources/C11/C11.txt)。

**CLAUDE_SUBAGENTS — Create custom subagents**

- 书目：Anthropic；出版年未核；Claude Code documentation；[原始页面](https://code.claude.com/docs/en/sub-agents)。
- 可支持：已有并发上限、maxTurns、partial output、resume、worktree等；TaskLoom应治理具体接入后的剩余问题。
- 限制：默认20不是所有入口硬cap；resume/workflow/team范围不同；官方能力不能自动保证每个版本无bug。
- 阅读与材料：正文相关段落已核；未独立复现实验；[D01.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/CCD01/D01.txt)。

**CLAUDE_AUTOCONTINUE — Settings reference — autoContinueAtUsageLimit**

- 书目：Anthropic；出版年未核；Claude Code documentation；[原始页面](https://code.claude.com/docs/en/settings-reference#autocontinueatusagelimit)。
- 可支持：2.1.234+已有默认true的订阅额度重置后自动继续，不能宣称不存在配额等待。
- 限制：适用claude.ai订阅/open session；不证明所有子agent恢复树自动完整；不能外推API RPM/TPM。
- 阅读与材料：正文相关段落已核；未独立复现实验；[D08.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/CCD08/D08.txt)。

**FUNCTION_CALLING — Function calling**

- 书目：OpenAI；出版年未核；OpenAI API documentation；[原始页面](https://developers.openai.com/api/docs/guides/function-calling)。
- 可支持：模型产生工具调用，应用执行工具并提交结果；说明调度控制点在runtime而不在模型文本本身。
- 限制：工具协议不提供完整任务DAG/未来耗时，不代表能任意暂停远端推理。
- 阅读与材料：正文相关段落已核；未独立复现实验；[R02.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/R02/R02.txt)。

### 问题单：只作有条件需求线索

**ISSUE_RESUME — [FEATURE] Mark a subagent killed by a usage limit as resumable, and keep it listed after a session restart**

- 书目：Issue author account WayneAl；2026；anthropics/claude-code issue #94222；[原始页面](https://github.com/anthropics/claude-code/issues/94222)。
- 可支持：用户报告SendMessage旧ID可恢复六子agent，支持区分恢复能力与恢复发现/状态可见性。
- 限制：用户自述非独立实证；单次token/findings差不作收益百分比；第三方插件评论不当维护者确认。
- 阅读与材料：issue正文与公开评论已核，未运行；[issue_94222.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/CC03/issue_94222.txt)。

**ISSUE_SLOT — Scheduled-task sessions intermittently freeze permanently on first/second tool call, never releasing their concurrency slot**

- 书目：未核署名，不补填；2026；anthropics/claude-code issue #92753；[原始页面](https://github.com/anthropics/claude-code/issues/92753)。
- 可支持：用户现场有长时间挂起占本地scheduled槽、其余任务global_limit拒绝的日志，作为监督和状态区分需求线索。
- 限制：根因审批等待/死锁未证；评论提出用户自加lease可能性；不是CPU/GPU拥塞实证，open不等无现成办法。
- 阅读与材料：issue正文与公开评论已核，未运行；[issue_92753.txt](E:/Project/OS-Project/research/agent-orchestrate/materials/L2_harness_tasks/CC06/issue_92753.txt)。

## 2. 最终写作的引用规则

1. **区分事实与设计。**引用支持“已有研究/产品实现某功能”，不支持“织序必然改善某百分比”；方案策略和阈值属于拟设计，实验尚未进行。
2. **区分限制合同。**API RPM/TPM、在途并发、总费用、订阅五小时/周用量分别记账；完成/取消不自动恢复已消费速率或费用。
3. **区分阅读与证据。**选读正文不是完整复现，产品文档不是性能证据，用户issue不是维护者确认。附件意见、评论自述和商业插件宣传不作独立实证。
4. **避免重复与虚假新颖性。**HiveMind与LiteLLM/Temporal已覆盖优先、依赖、预算和恢复；TaskLoom增量需由明确接入条件和强基线实验验证，不宣称首次Agent调度。

## 3. 元数据特殊事项

AIOS作者顺序按2026-09-29补核arXiv v5 citation字段：Wenyue Hua在Mingyu Jin之前；归档HTML正文呈现顺序不同，引用不混用。v1发表于2024、所读v5为2025。LLMCompiler材料沿用旧research/materials目录的PMLR正式页快照，未移动文件。深度研究中的GPU serving论文本表未扩展纳入，因为织序开题重点是受控runtime资源治理与步骤调度。

## 4. 算法出处补充（由主文分配最终数字）

### DRR — Efficient fair queuing using deficit round-robin

M. Shreedhar, G. Varghese. IEEE/ACM Transactions on Networking, 1996, 4(3):375–385. [DOI](https://doi.org/10.1109/90.502236)。Crossref题名为“queuing”（不是queueing），作者given仅提供M./G.，不擅自展开姓名。可作为欠额轮转经典出处；本次仅核书目，Crossref没有摘要。Agent场景中的成本估计、复杂度和等待界须另证，不能迁移网络队列保证。

### EDF — Scheduling Algorithms for Multiprogramming in a Hard-Real-Time Environment

C. L. Liu, James W. Layland. Journal of the ACM, 1973, 20(1):46–61. [DOI](https://doi.org/10.1145/321738.321743)。Crossref书目与摘要已核；摘要明确单处理器、按当前deadline动态赋优先级。开题可用作EDF依据，但未知时长、非抢占外部请求、多资源和在线DAG不能直接获得经典实时最优/可调度保证。

两条Crossref元数据与摘要直接归档在references_verified.json各自verified_metadata_excerpt字段；未另写文件。
