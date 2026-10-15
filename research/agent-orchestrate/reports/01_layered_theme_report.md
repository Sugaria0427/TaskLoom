# TaskLoom 四层痛点与统一编码主题报告

> 按本地资源、Harness任务、个人模型请求、平台多用户请求四层，归纳现有证据、根因和剩余问题。

**整理日期** 2026-10-14　**基础证据** 2026-09-29　**OpenClaw补充** 2026-10-14　**用途** 选题论证、需求分析与实验选材。

TaskLoom面向两个场景：个人电脑上同时运行多个Agent任务，以及受控应用平台接收多个用户的模型请求。项目固定模型能力与推理配置，优化工具执行、任务推进、请求准入和派发。基础调研沿用归档版本；OpenClaw补充依据2026-10-14的新材料。每条问题按对应来源的核查日期、版本和条件编码。

本文依次展开L1–L4，每层包含四个主题。主题编号采用`T-Lx-nn`，来源沿用P、A、CX等原编号。D/C/P/T/U分别表示已有覆盖、配置可解、部分未覆盖、结构性取舍、待确认；H/M/L分别表示条件性高、中、低剩余严重度。`NA`表示不进入剩余严重度排名。证据等级和严重度分别记录，详细定义见[编码规范](../materials/_index/codebook.md)。

优先处理有具体条件和机制依据的资源回收、所有权回滚与重试契约。个人请求公平、平台任务份额和两端动态反馈作为性能研究主线，通过成熟基线后的直接比较确定其价值。

## 1. L1：本地资源与工具执行

L1管理实际运行的工具、后代进程、资源容量和共享对象。逻辑上的Agent数量与设备真实占用不同，因此容量准入、冲突访问和退出回收需要分别检查。

本层四个主题的判断如下，表中的高严重度仅适用于注明的触发条件。

| 主题编号 | 问题 | 剩余状态与严重度 | 主要依据 |
| :--- | :--- | :--- | :--- |
| T-L1-01 | 工具需求异质与容量准入。 | T；OpenClaw特定现场条件性H。 | [L07](../materials/L1_local_resources/L07/L07.txt)、[C01](../materials/L1_local_resources/C01/C01.txt)、[C09](../materials/L1_local_resources/C09/C09.txt)。 |
| T-L1-02 | 特定结束路径没有完成后代回收。 | P；条件性H。 | [CX48524](../materials/L1_local_resources/CX48524/48524.txt)；[CX-SOURCE](../materials/L1_local_resources/CX-SOURCE/source_residency.txt)提供已有清理机制对照。 |
| T-L1-03 | 临时副本和任务产物持续占用空间。 | P；特定LFS案例H，worktree子项较轻。 | [CX34953](../materials/L1_local_resources/CX34953/34953.txt)、[CC14](../materials/L1_local_resources/CC14/issue_95644.txt)。 |
| T-L1-04 | 共享访问、重复初始化和分批申请缺少完整协议。 | P；已报告竞态M。 | [D3590](../materials/L1_local_resources/D3590/D3590.txt)、[DS_python_client](../materials/L1_local_resources/DS_python_client/source_python_client.txt)、[P10](../materials/L1_local_resources/P10/P10_text.txt)。 |

### 1.1 工具需求异质与容量准入

PeakBench把依赖可并行与资源容量可行分开评估；Ray文档也区分逻辑资源声明与物理资源限制。两者支持一个明确机制：允许两个工具同时运行，不等于内存、实例或输出空间足够。现有画像、并发上限和资源隔离已经提供基础控制，TaskLoom需要补测普通PC上真实工具的需求及并发干扰，确定按需求准入是否仍有收益。

这里的研究变量是启动安排和资源分配。工具执行时长包括外部等待，CPU、内存、磁盘和内部并行度应另行测量。[P16](../materials/L2_harness_tasks/P16/P16_text.txt)提供模型耗时占主导的样本，作为选择负载时的重要对照。



OpenClaw补充提供一个具体资源场景：在10GB VM、claude-cli及重工具并行的报告环境中，数量限制合规仍出现内存压力；10月现场更新也涉及build/test。当前配置与隔离源码支持数量和容量分别管理。该案例提高T-L1-01的条件性优先级，首版内置runtime的工具画像与收益通过实测校准。[OC-I161050](../materials/L1_local_resources/OC-I161050/issue_and_comments.md)、[OC-D12](../materials/L2_harness_tasks/OC-D12/sessions-and-subagents.md)

已选择OpenClaw作为个人端基底，机制、原生已解决问题、版本差异及接入路径集中在[方案报告第5章](02_solutions_and_integration.md#5-openclaw基底专题机制剩余问题与taskloom接入)。

### 1.2 工具进程树与退出回收

Codex #48524报告app-server的特定EOF退出路径过早终止MCP清理，报告者在相应版本复测；同一报告中的exec路径能够清理成功。已归档源码包含进程组终止和驻留淘汰，这将问题定位为具体结束路径的交接，而不是普遍缺少进程管理。

TaskLoom的对应需求是记录自己拥有的工具树，分别确认取消请求、实际停止与容量归还。优先实验应覆盖正常完成、取消、超时、EOF与执行器崩溃，比较真实后代残留和回收时间。

### 1.3 临时产物与输出存量

Codex #34953的证据涉及特定Git LFS文件的后台处理和临时副本增长；Claude #95644涉及只读worktree结束后没有回收。前者有耗盘报告，后者提供清理遗漏案例，两者按具体后果分别判断严重度。

根因分析聚焦产物由谁持有、重复生成如何合并、结束后何时可回收。TaskLoom应管理实际产物预算和引用关系，对大段工具输出采用有界消费与文件引用。空间统计必须包含仍在被后续步骤使用的结果。

### 1.4 共享访问与分批资源申请

DeepSeek #3590的并发启动记录与选读Python源码支持检查后创建的竞态窗口。Playwright的profile隔离和Claude的worktree提供现成冲突处理办法；这些能力应进入基础方案。共享文件的修改和测试，还需要通过任务依赖与访问协议维持顺序。

银行家算法属于该主题的候选分配方法：受控阶段声明可信最大需求，每次分批申请先检查安全状态。[OS21](../materials/L1_local_resources/OS21/S21_bankers_algorithm.txt)、[OS22](../materials/L1_local_resources/OS22/S22_deadlocks_course.txt)提供算法条件，[OS23](../materials/L1_local_resources/OS23/S23_priority_inheritance.txt)支持优先级反转的机制分析。该方法由L1执行，L2提供阶段信息；它将在受控实例名额上独立验证。


---

## 2. L2：Harness与Agent任务管理

L2管理根任务、子Agent、步骤依赖和执行记录。它的主要问题是让就绪工作得到推进，并让结果、终态和资源拥有者保持可核对的关系。

本层的案例按机制合并，产品名称只用于标明证据来源。

| 主题编号 | 问题 | 剩余状态与严重度 | 主要依据 |
| :--- | :--- | :--- | :--- |
| T-L2-01 | 失败消息留下所有权依赖，工作结束后无法结算。 | P；条件性H，E2。 | [D7936](../materials/L2_harness_tasks/D7936/D7936.txt)、[DS_continuation](../materials/L2_harness_tasks/DS_continuation/source_continuation.txt)、[DS_activation](../materials/L2_harness_tasks/DS_activation/source_activation.txt)。 |
| T-L2-02 | 唤醒丢失、挂起占槽或后台事件拖慢控制消息。 | P；总体M，具体阻断条件单列。 | [D274](../materials/L2_harness_tasks/D274/D274.txt)、[D4952](../materials/L2_harness_tasks/D4952/D4952.txt)、[D1316](../materials/L2_harness_tasks/D1316/D1316.txt)、[CC06](../materials/L2_harness_tasks/CC06/issue_92753.txt)。 |
| T-L2-03 | 父子取消、旧结果、真实停止与界面终态脱节。 | P；M，部分根因待核。 | [CC09](../materials/L2_harness_tasks/CC09/issue_95138.txt)、[CC11](../materials/L2_harness_tasks/CC11/issue_92716.txt)、[CX49131](../materials/L2_harness_tasks/CX49131/49131.txt)。 |
| T-L2-04 | 已有成果的发现、父子续接和最终交付不完整。 | P；M。 | [CC02](../materials/L2_harness_tasks/CC02/issue_94577.txt)、[CC03](../materials/L2_harness_tasks/CC03/issue_94222.txt)；[A10](../materials/L2_harness_tasks/A10/A10.txt)、[CCD08](../materials/L2_harness_tasks/CCD08/D08.txt)为已有恢复对照。 |

### 2.1 所有权交接与失败回滚

DeepSeek #7936描述兄弟消息被拒后仍留下ownership hold，导致子任务完成却不能settle。归档源码支持先改变关系、拒绝后回滚不足的机制链，社区分支测试与本项目的静态阅读分别记录。该问题需要先验证消息，再原子更新所有权，并在失败路径完成回滚。

历史session写锁问题则已经有当前归档master中的SessionWriteLease覆盖。[D4178](../materials/L2_harness_tasks/D4178/D4178.txt)、[D5460](../materials/L2_harness_tasks/D5460/D5460.txt)和[DS_lease](../materials/L2_harness_tasks/DS_lease/source_lease.txt)作为已覆盖子机制保留，退出当前高严重度排序。

### 2.2 等待唤醒与控制通道

DeepSeek的follow-up记录提供丢失唤醒线索，后台事件流案例则指向缺少过滤与背压。Codex本地RPC占槽、Claude scheduled run长期无进度的报告呈现相似阻断后果，具体根因各自记录，避免用同一个“死锁”解释全部现象。

TaskLoom需要区分运行、等待审批、等待额度和等待工具，并用可达的控制通道传递停止、完成与资源释放。事件到达后重新检查就绪条件，代际标识用于处理重复或迟到唤醒。

### 2.3 父子取消与终态一致性

Claude #95138的父停子续案例仅有一次观测，独立复现尝试没有触发同样嵌套条件；#92716则说明正常完成hook不能直接当作所有结束路径的权威账本。Codex #49131只有工具卡片仍显示Running，实际进程已经结束，应归入状态一致性。

因此，TaskLoom分别记录结果可发布、物理执行停止、资源回收和费用结算。重试产生新的Attempt，旧结果不覆盖新结果，旧尝试的真实消耗仍按一次性结算处理。无法确认外部效果时保留UNKNOWN并进入对账流程。

### 2.4 检查点与完整任务续作

LangGraph已经提供检查点、pending writes与恢复；Claude官方资料和案例也说明部分成果能够保留、子Agent能够resume。剩余需求集中在根任务成果索引、父子分支发现、恢复入口和最终验收之间的衔接。

订阅额度中断案例用于定义续作和交付需求，API请求速率另归L3。恢复时核验输入版本与已有结果，必要步骤继续执行，已发生但未确认的外部副作用先对账。


---

## 3. L3：个人模型请求与额度

L3安排一个用户的就绪模型请求何时发送，管理个人任务预算和尝试记录。它与L4具有相近的队列方法，区别在于公平范围和可见信息。

本层将合同规则、实现缺口与公平假设分别编码。

| 主题编号 | 问题 | 剩余状态与严重度 | 主要依据 |
| :--- | :--- | :--- | :--- |
| T-L3-01 | Retry-After、退避上限与任务等待期限不一致。 | P；M，E2；配置可缓解部分另标C。 | [D892](../materials/L3_client_requests/D892/D892.txt)、[D3128](../materials/L3_client_requests/D3128/D3128.txt)、[DS_retry](../materials/L3_client_requests/DS_retry/source_retry.txt)、[C04](../materials/L3_client_requests/C04/C04_browser.txt)。 |
| T-L3-02 | 安全费用预留与请求可用性存在取舍。 | T；M，提案证据E3。 | [A21](../materials/L3_client_requests/A21/A21.txt)、[A29](../materials/L3_client_requests/A29/A29.txt)、[A05](../materials/L4_platform_requests/A05/A05.txt)。 |
| T-L3-03 | 速率窗口、在途槽和消耗型预算需要不同规则。 | C；成熟基础控制，NA。 | [A01](../materials/L3_client_requests/A01/A01.txt)、[A02](../materials/L3_client_requests/A02/A02.txt)、[A14](../materials/L4_platform_requests/A14/A14.txt)、[A19](../materials/L4_platform_requests/A19/A19.txt)。 |
| T-L3-04 | 子Agent扇出是否放大根任务份额。 | U；强基线后的损失待测，NA。 | [A06](../materials/L3_client_requests/A06/A06.txt)、[A20](../materials/L2_harness_tasks/A20/A20.txt)、[D7098](../materials/L2_harness_tasks/D7098/D7098.txt)；[A24](../materials/L4_platform_requests/A24/A24.txt)为公平先例。 |

### 3.1 重试提示与等待预算

DeepSeek的D892、D3128、D3852和D7926属于同一个重试机制家族：服务端等待头、适配器错误信息和内部重试规则没有完整贯通。配置调整能够处理一部分条件，头信息丢失和不同层总时限不一致需要接口修正。

TaskLoom应统一保存等待提示、根任务期限和Attempt身份，再安排延迟重试。请求发出后超时进入不确定状态时，先确认重试安全性与真实消耗。

### 3.2 费用预留与实际结算

LiteLLM费用预留提案说明最坏费用上界与请求可用性的取舍。严格预留能够保护硬预算，也可能暂时占住实际未消耗的额度；估计预留允许更多请求启动，需要明确误差和风险边界。

本项目将该问题作为账本策略实验，保持模型、计价和输出上限一致，同时统计实际费用、未结算预留和合格完成任务。提案与讨论提供设计依据，发布状态保留在来源记录中。

### 3.3 速率窗口与在途数量

提供商文档规定请求数、输入输出token、作用域和恢复规则；网关不同路径的计数时机也可能不同。这一主题首先属于契约实现：时间速率按窗口或补充规则恢复，在途槽在确认结束后释放，已消费费用按实际结算。

成熟限流正确配置进入全部对照组。TaskLoom将根任务归属接入同一账本，重点检查真实在途记录、系统外调用影响和多约束原子放行。

### 3.4 根任务公平与子Agent扇出

请求级公平与根任务级公平使用不同的服务单位。HiveMind、Temporal及现成Harness已经具备优先级、DAG、公平或并发控制；现有材料尚未量化这些合理措施之后，个人账户内仍有多少长短任务失衡。

TaskLoom以根任务拥有份额、子Agent继承份额作为候选策略。成本DRR、等待老化和有明确期限时的次级排序，将通过同工作量、不同扇出规模的对照实验验证。


---

## 4. L4：平台多用户请求与两端协同

L4是本项目拥有的应用API网关。它在固定的上游条件下管理多用户接纳、排队和派发，并与个人端交换有时效的必要信息。

平台层当前最重要的结论是：基础控制已有成熟方案，TaskLoom的任务级公平和动态协同需要在这些方案之上验证增量。

| 主题编号 | 问题 | 剩余状态与严重度 | 主要依据 |
| :--- | :--- | :--- | :--- |
| T-L4-01 | 用户、根任务与请求的服务份额如何衔接。 | U；实际失衡待测，NA。 | [A24](../materials/L4_platform_requests/A24/A24.txt)、[A26](../materials/L4_platform_requests/A26/A26.txt)、[C10](../materials/L4_platform_requests/C10/C10.txt)。 |
| T-L4-02 | 会话关联没有自动提供完整调度元数据。 | T；接口事实E1，性能损失待测。 | [R01](../materials/L4_platform_requests/R01/R01.txt)、[R02](../materials/L2_harness_tasks/R02/R02.txt)、[R04](../materials/L2_harness_tasks/R04/R04.txt)。 |
| T-L4-03 | 等待容量和派发容量需要明确边界与背压。 | C；成熟基础控制，NA。 | [C08](../materials/L4_platform_requests/C08/C08.txt)、[A25](../materials/L4_platform_requests/A25/A25.txt)、[A17](../materials/L3_client_requests/A17/A17.txt)。 |
| T-L4-04 | 两端动态反馈是否改善完整任务完成。 | U；核心研究假设，NA。 | [A06](../materials/L3_client_requests/A06/A06.txt)及运行时接口；需要直接实验。 |

### 4.1 多用户与根任务服务份额

Temporal、LiteLLM和Kubernetes APF提供优先级、公平、借用或准入的先例。TaskLoom比较的是先分用户、再分用户内根任务的成本公平，防止扇出改变公平单位，并为无工作用户的空闲机会提供借用规则。

当前材料不足以判断个人端与平台端哪一侧排序更差。相同负载应分别记录个人发送前等待与平台接纳后等待，依据真实损失决定优化重点。

### 4.2 平台可见信息与任务元数据

模型API可以接收调用方选择的历史，也可以通过会话对象或previous_response_id关联状态。两种方式都需要受控运行时另外提供根任务归属、步骤、期限和本地状态，才能支持完整任务调度。

平台先使用认证身份、等待时间、请求规模和实际用量进行基础调度。任务级策略再接入最小元数据；身份由网关认证，紧急份额由平台策略约束，状态摘要带有效期。

### 4.3 接纳、过载与明确背压

有界队列、在途上限和Retry-After已有成熟实现。TaskLoom把接纳排队与真正派发分别记录，并在容量不足时提供明确反馈，使个人端能够安排后续发送。

队列过载控制属于基础方案。性能实验将拒绝、超时与未完成任务全部保留在分母，避免通过少接任务获得漂亮的延迟统计。

### 4.4 两端动态反馈与阶段协调

个人端知道本地工具是否积压，平台知道全体用户及派发压力。模型结果可能产生工具工作，工具结果又会生成下一轮模型请求，这为阶段协调提供了机制依据；额外收益仍由实验确定。

直接比较两端分别优化的B11与增加动态反馈的B11+FB。两组保留相同身份、静态信息和策略，仅切换动态反馈，并统计完整任务完成、各段等待、公平和反馈开销。结果将决定协同在什么条件下值得启用。

本报告给出问题与证据，已有能力和接入选择见[现有方案报告](02_solutions_and_integration.md)，实现与评价方法见[OS设计及验证报告](03_os_design_and_validation.md)。
