# Web 反爬与反 Agent 技术：OS 课设方法、创新边界与实验方案

调研日期：2026-09-27。本文针对“以静态网站模板展示、服务端响应模块保护正常访问”的候选选题，不代表已确认实现范围。原始网页材料、论文正文提取及检索记录分别存入 `research/materials/web_bot_control_2026-09-27/` 下的 `web/`、`extracted/`、`metadata/`。

## 1. 结论与适用范围

**选题适合操作系统课设，但“IP 行为识别＋差异限流”本身已经有研究和产品实现。更值得验证的是：在身份识别不完全准确、请求成本不等、源站负载突变时，如何以有限状态和计算开销保护正常会话的服务质量。**

研究对象是网站入口的应用层准入与响应调度模块。静态模板作为被保护网站和展示界面，模块部署在服务器或反向代理入口。仅在浏览器页面里运行脚本，无法可靠阻止脚本直接访问源站，也无法收回已经消耗的后端处理资源。

必须分清四个概念：

| 概念 | 目标 | 与本课设的关系 |
|---|---|---|
| 内容抓取政策 | 网站允许哪些自动化主体获取哪些内容 | robots 与身份验证可提供政策输入，不直接消除过载 |
| 自动化识别 | 判断请求更像人、传统脚本还是浏览器 Agent | 提供带误差的信号，不作为绝对真值 |
| 应用层过载保护 | 限制昂贵工作进入服务，分配有限资源 | OS 方法的主要落点 |
| 链路/网络层抗 DDoS | 防止上游带宽、网络设备等被耗尽 | 超出单机响应模块能力，不能用后端调度承诺解决 |

“Agent”不是服务器可直接识别的协议类型。AI 训练爬虫、普通脚本、用户授权浏览器 Agent、搜索与监控程序，可能有不同价值和成本。自动化不等于恶意，真人也可能产生高成本请求。

## 2. 现有技术已经做到什么

### 2.1 访问政策与可验证身份

**robots.txt：**RFC 9309 明确写道，规则不是访问授权机制。它依赖自动客户端遵循，不能代替实际访问控制。[D1]

**AI crawler 管理：**Cloudflare AI Crawl Control 已支持爬虫访问统计、按爬虫允许/阻断和 robots 遵循情况管理。部分识别依赖公开 User-Agent；产品存在不等于能可靠辨认所有不自报身份的浏览器 Agent。[D2]

**签名与已验证 bot：**Web Bot Auth 使用 HTTP 消息签名验证自动化主体身份；相关架构仍引用 IETF drafts，不能写成已定稿的新 RFC。签名证明请求与某个持钥主体相关，不证明资源消耗低，也不证明背后终端用户的行为可接受。Cloudflare Verified bots 产品分类还额外要求诚实身份和非滥用行为，应与单纯密码学签名区分。[D3]

对课设的含义：可信自动化可以有独立的服务政策与预算，但不能无限放行；自报 User-Agent 只能作为弱信号。身份、行为和服务器负载应分别建模。

### 2.2 被动识别、浏览器信号与行为评分

现代 Bot Management 已组合 IP/网络信誉、HTTP/TLS 特征、会话信号、浏览器指纹、JavaScript 探测和行为模型。Cloudflare 官方还使用会话 cookie 平滑评分，并在规则中将 bot score 与 URL、会话 cookie 等组合。[D4–D5]

不能把“按 IP 计数”当作所有现有方案的上限：Cloudflare 已有 `IP with NAT support`，通过 cookie 区分部分共用出口的访问者。文档也明确，缺少 cookie 的访问者仍可能共享计数桶，出现 NAT 误伤。[D5]

不同信号有不同可见范围。源站能看到路径、请求间隔和响应成本；鼠标、滚动等需要客户端采样，不能从普通访问日志凭空得到。JS 检测通常需要先返回 HTML；首次请求没有完整行为证据，合法客户端也可能禁用 JS 或无法执行。JS 成功不证明真人，失败不证明恶意。[D4]

**本课设可以采用轻量评分，但不要把分数直接解释为已校准的“真人概率”。** 如果使用学习模型，应另测校准和跨场景泛化；如果只是规则分数，明确称其为启发式风险分数。

### 2.3 主动挑战与增加访问成本

CAPTCHA、Managed Challenge、Turnstile、PoW 等技术通过交互、浏览器环境或计算成本增加自动访问负担。Turnstile 已包含非交互式挑战、工作量/空间证明、浏览器 API 和行为信号，并支持 invisible 模式；“动态无感挑战”不能作为全新机制。[D6]

Anubis 是开源 PoW 防护实例。其基本思想是让客户端承担求解成本、服务端低成本验证；官方也承认可能影响 Internet Archive 等正常自动化。[D7]

PoW 证明完成了一定计算，不证明是人。Invisible 描述交互形式，不保证没有等待、客户端计算或兼容性成本。若以“正常用户尽量无感”为目标，建议挑战只作可选对照/后续模块，不把它作为第一版核心。

### 2.4 准入、限流、缓存与动态服务控制

| 现有机制 | 已核验来源 | 不能当作原创的内容 |
|---|---|---|
| 按 key 限速、突发额度、并发约束 | NGINX `limit_req`、`limit_conn` | IP 表、漏桶、延迟/拒绝、固定并发 |
| 成本与行为联合限流 | Cloudflare complexity-based rate limiting 与 best practices | 按成本扣配额、bot score 联动、会话代替纯 IP |
| 负载反馈准入 | Envoy Adaptive Concurrency/Overload Manager；Breakwater | 随延迟或内存/CPU 压力调整准入 |
| 公平隔离与重复工作减少 | Kubernetes APF；NGINX `proxy_cache_lock` | 多级公平队列、成本份额、空闲容量借用、相同缓存填充合并 |

NGINX 的限流 key 可以配置，并非只能按 IP。`limit_conn` 在 HTTP/2、HTTP/3 下把并行请求作为独立连接计数。缓存填充锁受超时等设置影响，不代表任何时候都只有一个回源请求。[D8]

Envoy 自适应并发利用完成请求延迟反馈调整在途请求数；文档明确存在观测和控制前提，周期测量 minRTT 时可能增加 503。Overload Manager 保护的是代理自身资源，后端容量应单独观测。此次 latest 页面显示 1.40.0-dev，正式实现前应固定版本。[D9]

Kubernetes APF 已把请求分级、级内公平、shuffle sharding、空闲容量借用和按估算成本占用多个 seats 结合起来。它是机制先例，不是公共网站反爬产品；它可利用用户/namespace 等身份，匿名公网身份更不可靠。[D10]

对于纯静态网站，CDN/缓存、压缩和合理并发是必须先比较的方案。静态文件已在缓存中时，复杂行为识别可能增加 CPU。相同缓存 key 的合并必须遵守授权、Cookie、Vary 和内容语义，不能混合不同用户的私有响应。

## 3. 学术证据与局限：不要只看漂亮数字

### 3.1 直接接近“IP—行为表”的研究

**Logrip: Protecting Small Organizations from AI Bots with Hierarchical IP Hashing（2025 预印本）**与本选题高度接近：通过访问日志、IP/子网层次聚合、时间行为分析保护小型组织网站。[P1]

但其 §8 明确承认缺少经过验证的人机真值。案例中 80–95% 自动访问是估计，过滤大量请求并不能独立证明真人体验得到保障。论文也没有证明所有自动流量由 LLM Agent 驱动。它适合说明场景和已有方案，不适合给本项目预告提升比例。

**Shy Guys（2026 预印本，页面称投稿 IFIP TMA）**使用 User-Agent 一致性和 favicon 访问特征进行低成本检测。论文报告约 67.6–67.7% 检出与 3% 假阳性，但标签由 LMS 人类流量和 honeypot 流量组合构造，部分评估参考 Cloudflare，仍有选择与标签假设。favicon 缓存、IP 变化和旧浏览器等影响泛化。[P2]

这两项工作说明“便宜的日志特征能提供线索”，也说明高检出、低开销、低误伤并不能自动同时成立。

### 3.2 浏览器 Agent 的新特征与泛化问题

**FP-Agent（2026 预印本）**在受控网站上比较七类浏览器 Agent 与人类，发现行为特征比单纯浏览器指纹更有区分力。正文 §6 讨论闭集分类、有限设备/任务覆盖和分布变化，不能把受控实验的识别表现当作互联网部署结果。与 Cloudflare 的个案对比只针对论文所测配置和时间，不代表所有商业套餐能力。[P3]

**What Does It Take to Detect an AI Agent?（2026 arXiv，记录称 workshop 接收）**研究低维行为特征。作者明确，关键信号是所测浏览器自动化 API 的执行痕迹，不是“AI 推理本质”。正文限制包括单一模型与 Playwright/MCP 路径、人与 Agent 的任务分布差异，以及未实测的跨框架泛化。[P4]

另读了 **On the Internet, Nobody Knows You're an LLM Bot（2026 预印本）**摘要作为范围线索；它研究六个 Web Agent 和多层指纹。未据此复述绕过方法，也未将其摘要结论作为部署有效性保证。[P5]

**结论：不建议把“彻底识别所有 Agent”作为课设主目标。** 对 OS 项目更合适的方式，是承认识别有误差并验证调度在误差下是否仍然有用。

### 3.3 成熟系统研究已经有反馈式过载控制

**Breakwater（OSDI 2020）**按服务端排队延迟发放准入 credits，并用主动队列管理应对短时过载。它研究微秒级数据中心 RPC，合作客户端和网络环境与公共匿名网站不同；不能搬用其性能数字，但它足以否定“根据实时排队动态准入是新想法”。[P6]

**Good Bot, Bad Bot: Characterizing Automated Browsing Activity（IEEE S&P 2021）**的标题、作者、会议信息和 DOI 经 Crossref 核验。本文未读取其全文，不引用其具体实验数字。[P7]

本次没有证据证明“风险、成本、公平和反馈第一次被组合”。创新表述应限定为待检验的具体机制和实现贡献。

## 4. OS 方法如何形成可交付系统

### 4.1 建议的系统边界

候选名称：**面向自动化访问的网站资源保护与请求调度系统**。场景可先选小型公共资料站；保留用户提出的静态站点模式，先测缓存后的真实瓶颈。如果确实是静态文件，只研究其连接、内存或发送带宽约束，不为制造收益偷偷添加昂贵动态接口。若后续明确加入检索/生成接口，将其单独标为扩展场景。

逻辑路径：

```text
正常浏览器 / 合作爬虫 / 脚本与 Agent
                  ↓
入口响应模块：廉价检查、缓存判断、有界状态查询
                  ↓
风险信号 + 路径成本估计 + 代理/源站负载
                  ↓
准入与公平队列：执行 / 有界等待 / 快速拒绝
                  ↓
静态服务或后端 worker

旁路：行为聚合、指标采样、持久化日志、守护进程
```

JS 特征从浏览器回传，不应阻塞所有首次访问；无法取得时归为未知，而不是默认恶意。客户端采样自身会增加请求和 CPU，必须计入总成本。源站回报成本、代理头和用户身份仅在可信配置范围内采用，不能信任客户端随意声明的“低成本/真人”头。

### 4.2 四组 OS 机制及合理算法

| 机制组 | 建议采用 | 为什么需要 / 边界 |
|---|---|---|
| 准入与调度 | 有界生产者—消费者队列、信号量、成本感知 DRR 类调度、等待老化 | 控制真正消耗资源的在途工作，避免长任务或大流独占；主要非抢占地决定启动顺序，不随意暂停已运行 HTTP 事务 |
| 并发与通信 | 接入/worker/统计进程分工，消息队列或 Unix socket，互斥锁/共享快照 | 保证多 worker 额度不重复发放、统计可追溯；不必为了覆盖知识点同时堆所有 IPC |
| 状态与置换 | 固定容量访问记录、TTL、LRU/CLOCK、日志轮转与文件锁 | 防止来源数量增加耗尽内存；淘汰记录不能等同于清零所有配额，否则产生反复获得新额度的问题 |
| 守护与恢复 | 独立守护进程、心跳/退出检测、信号、限频重启、配置和计账快照 | 课件 pp.7–12 明确要求守护进程；实测 worker 崩溃、挂起和重启时的服务影响 |

DRR 本来按服务量分配份额。用于 HTTP 前应声明成本单位：静态内容可按响应字节，CPU 密集接口可按估计 CPU 时间；不能把未归一化的字节和毫秒直接相加。总耗时也不等于 CPU 成本，它可能含排队和 I/O。预计成本用于准入，结束后可校正计账，并用并发/超时硬上限约束低估风险。

纯短时静态请求不适合硬套可抢占 MLFQ 或 EDF。若没有业务截止时间，就不要从访问者身份凭空造截止期；可把正常页面的目标响应时限作为评价约束。

“延迟响应”不是天然减压：等待若仍占线程/连接/内存就可能扩大问题。队列必须有容量、超时和断连清理；等待占用也必须统计。按适用语义返回 429/503 与 Retry-After；对不遵守退避的客户端，本地仍要廉价拒绝，不能依赖其合作。

## 5. 可以尝试的创新假设：优先级与验证方式

### 5.1 优先主线：识别误差下的有界差异服务

**现实矛盾：**直接按机器人分数封禁容易误伤；完全忽略信号又可能让大量自动请求与正常浏览争抢资源。

**候选机制：**把行为分数只作为有限幅度的服务权重调整，将可信身份、成本和等待时间分开处理；给未知类别分配一个总量受限的服务池，在低负载时允许利用空闲容量，过载时先保障明确政策内的正常业务。用迟滞和最小调整间隔降低权重抖动。

重要边界：未知池的保底是**类别总份额**，不是每个新 IP/新 cookie 无限领取独立保底。匿名身份可以大量变化，无法在没有额外约束时保证“每个真实用户公平”。NAT 下缺少独立会话信息时，也不能保证辨认每个用户。方案应明确退化策略，而不是声称解决了匿名身份问题。

**应验证的增量：**在相同检测器和相同资源下，与“固定阈值封禁/固定分层限流”及“不使用行为的成本公平调度”相比，识别误差上升时，正常会话的按时成功率是否下降更慢，最差群体是否改善。该假设尚未证明学术首创。

### 5.2 可选深入点：检测器也纳入资源预算

**现实矛盾：**负载越大，采集/分析所有访问行为越可能成为防护模块自己的负担；但完全停止分析又会减少判断依据。

**候选机制：**同步路径只做便宜判断，详细分析进入有界异步队列；重用有 TTL 的评分，在计算预算内优先更新陈旧或不确定状态，CPU/队列紧张时降低详细分析频率。给过期分数明确置信衰减，不能永远沿用旧结论。

**应验证的增量：**在相同保护效果或正常访问约束下，模块总 CPU/内存是否降低；漏检是否恶化，攻击流量变化时是否反应太慢。已有系统普遍存在分层检测，因此“异步检测/缓存分数”本身不新，贡献取决于预算分配规则和实验。

需避免反馈偏差：被提前拒绝的请求没有完整交互行为，放行样本不能代表全流量。保留缺失/未知标记，实验中单独检查这种选择偏差，不能把缺失交互当成必然恶意。

### 5.3 可选深入点：有限访问表下的服务稳定性

**现实矛盾：**流量来源不断增加时，逐 IP 精细状态可能占满内存；粗暴按子网聚合又可能误伤共用出口的人。

**候选机制：**精细会话状态与固定总量的粗粒度统计分层保存，比较 TTL/LRU/CLOCK 等淘汰策略；详细记录淘汰后仍受全局/端点预算约束，避免状态释放同步解除所有访问约束。保护活跃正常会话的状态是否有意义，应通过对照检验，不能一概长期保留所谓高信誉身份。

**应验证的增量：**相同内存预算下，状态周转速度、误限流、额度重置异常和锁竞争是否改善。Logrip 已有 IP 层次聚合，不能把分层 IP 表当成新发明。

**范围建议：第一项作为主线，第二或第三项最多选一个扩展。** 缓存、限流和守护恢复是基础能力；不要把全部模块都写成创新点。

## 6. 怎样证明比现成方案更好

### 6.1 强基线与公平比较

所有组使用相同静态文件、缓存配置、硬件上限、客户端负载和连接协议。无保护只作展示，不作为主要成绩来源。

| 对照组 | 配置 | 排除的伪收益 |
|---|---|---|
| B1 成熟固定控制 | 调参后的 NGINX 风格按 key 限速、全局并发、有界等待 | 不是只打败完全不限流的服务器 |
| B2 行为分层控制 | 同一评分器，固定阈值与差异限额 | 分离检测收益和调度收益 |
| B3 负载/成本控制 | 不用行为信号，使用反馈准入和成本公平调度 | 证明行为信息真的有额外价值 |
| P 候选方案 | 与 B3 同样的底层机制，加入受限行为权重与选定扩展 | 量化具体增量，再做逐项消融 |

可直接使用成熟产品实现可比基线，或复现其核心机制并如实标注；不能将简化版称为完整 Cloudflare/Envoy，也不能用无法获得的商业分数作为第一版必要条件。调参只用训练/校准负载，测试流量和故障条件独立保留。

### 6.2 工作负载与真值

四类负载足够覆盖关键假设：

1. **正常使用：**普通浏览、并行资源加载、共享 NAT、首次访问与无 JS/cookie 条件，覆盖不同缓存命中情况。
2. **合理自动化：**合作搜索/监控或用户授权 Agent，有明确的允许政策与预算。
3. **过量自动化：**突发和持续抓取、多个低速来源的叠加、少量高成本请求；这些标签由实验生成器/受控参与者记录，不能靠待评估检测器自产。
4. **系统边界：**正常热门事件、负载突变、来源状态周转、worker 故障与重启；区分全站流量上涨和恶意访问。

回放真人浏览记录的脚本仍然是自动化执行路径，不能当作“浏览器指纹检测的人类真值”。调度实验可以使用带业务标签的回放；识别实验需要独立真实用户样本，或明确只验证模拟误差条件。按会话/用户/时间分割训练测试，避免同一人的相邻请求泄漏到两边。

不假设人类总低频、机器总高频，也不把 CDN/厂商的 bot score 当金标准。无真值的公开日志只能支持访问形态、容量校准等结论，不能独立证明误伤率。

### 6.3 宏观指标与一个月仿真

| 指标组 | 推荐指标 | 说明 |
|---|---|---|
| 正常使用体验 | 正常会话按时成功率、P95/P99 完成时间、超时与误拒绝 | 分母包含所有到达的正常会话，不能仅统计放行成功者 |
| 资源与有效服务 | 源站及保护模块总 CPU、峰值内存、出口流量、超载持续时间；按时成功业务量 | 大量快速 429 的“高吞吐”不是用户收益 |
| 公平与代价 | NAT/未知群体最差等待、允许自动化服务量、挑战比例/等待 | 不以封死所有机器来人为改善真人指标 |
| 可靠性与自身开销 | 故障恢复时间、额度一致性、状态淘汰量、锁等待/采样开销 | 不能只展示守护进程启动成功，要注入进程异常 |

“人类无感”改成事先规定的体验约束，阈值根据网站任务确定。可用“在正常会话按时成功率及误拒绝约束下，最大化有效完成量”，但约束值和提升幅度不能先编造。

先真实测量缓存命中/未命中、文件大小和并发条件，再校准离散事件模型模拟 30 天。报告常态、热门事件和异常时段分别的结果，不能只展示月平均。压缩仿真不等于把真实网络压测加速 30 倍；CPU/带宽/缓存关系需在模型中保留并通过少量真实运行校验。使用配对负载、多随机种子与置信区间。

## 7. 交付建议与最终取舍

建议交付四部分：服务端响应/调度模块；静态模板上的被保护网站和可视化面板；可复现实验负载与策略切换；体现守护、IPC、同步和文件处理的运行记录及报告。第一版不需要训练大型分类器，也不需要自行实现完整 TLS 指纹识别或修改内核。

**给老师的表述：**本项目研究自动化访问与正常浏览混合时，小型网站如何在有限资源下稳定提供服务。系统利用带误差的访问行为信息、请求成本和服务器负载，对请求进行有界准入与公平调度，并通过多进程、同步通信、状态置换和守护恢复实现。实验对比成熟限流、纯负载控制与候选策略，评价正常会话等待、误伤和服务器开销。

相比前一个 Agent 工具选题，这一方案的现象更容易展示，OS 映射更直接；但成熟程度也更高。**课程级创新可以是明确约束下的机制改进与完整实证；目前不能宣称发现了无人研究的反 Agent 调度空白。** 若合理缓存与简单并发控制已经足够，则应接受调度增量有限，避免人为制造高成本接口来放大结果。

## 8. 核心来源与阅读范围

### 8.1 官方协议与反自动化文档

**D1** IETF. RFC 9309, *Robots Exclusion Protocol*, 2022。[原文](https://www.rfc-editor.org/rfc/rfc9309.html)。已读授权边界与安全说明。

**D2** Cloudflare. [AI Crawl Control](https://developers.cloudflare.com/ai-crawl-control/)、[Manage AI crawlers](https://developers.cloudflare.com/ai-crawl-control/features/manage-ai-crawlers/)。已核验功能和识别条件，非独立效果测评。

**D3** Cloudflare. [Web Bot Auth](https://developers.cloudflare.com/bots/reference/bot-verification/web-bot-auth/)、[Verified bots](https://developers.cloudflare.com/bots/concepts/bot/verified-bots/)。已读身份验证、资格与转移信任边界。

**D4** Cloudflare. [Detection engines](https://developers.cloudflare.com/bots/concepts/bot-detection-engines/)、[Bot scores](https://developers.cloudflare.com/bots/concepts/bot-score/)、[JavaScript Detections](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/javascript-detections/)。产品文档，所列异常检测子引擎正在弃用，本文不推荐将其当最新能力。

### 8.2 准入、挑战与过载保护

**D5** Cloudflare. [Rate limiting parameters](https://developers.cloudflare.com/waf/rate-limiting-rules/parameters/)、[Best practices](https://developers.cloudflare.com/waf/rate-limiting-rules/best-practices/)。已核验成本计量、NAT 支持、会话与 bot score 联动；部分功能受套餐限制。

**D6** Cloudflare. [Turnstile](https://developers.cloudflare.com/turnstile/)。已核验挑战组成，未测量客户端开销。

**D7** Techaro. [Anubis README](https://github.com/TecharoHQ/anubis)、[设计源文件](https://raw.githubusercontent.com/TecharoHQ/anubis/main/docs/docs/design/why-proof-of-work.mdx)、[PoW 配置源文件](https://raw.githubusercontent.com/TecharoHQ/anubis/main/docs/docs/admin/configuration/challenges/proof-of-work.mdx)。文档站实际返回挑战页，转读官方公开源文件；没有执行挑战或运行软件。

**D8–D10** NGINX [限速](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html)、[并发](https://nginx.org/en/docs/http/ngx_http_limit_conn_module.html)、[缓存填充锁](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_cache_lock)；Envoy [Adaptive Concurrency](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/adaptive_concurrency_filter.html)、[Overload Manager](https://www.envoyproxy.io/docs/envoy/latest/configuration/operations/overload_manager/overload_manager)；Kubernetes [API Priority and Fairness](https://kubernetes.io/docs/concepts/cluster-administration/flow-control/)。已读关键机制与限制，未部署复现。

### 8.3 当前自动化识别研究

**P1** Hoetzlein, R. C. *Protecting Small Organizations from AI Bots with Logrip: Hierarchical IP Hashing*. 2025 预印本。[arXiv](https://arxiv.org/abs/2508.03130)。读案例、方法和 §8 局限；缺乏可靠真值。

**P2** Van Boxem, R., Barbette, T., Pelsser, C., & Sadre, R. *Shy Guys: A Light-Weight Approach to Detecting Robots on Websites*. 2026 预印本。[arXiv](https://arxiv.org/abs/2603.28546)。读方法、标签构造和评估限制；投稿状态不是会议录用。

**P3** Wang, E., Shafiq, Z., & Vekaria, Y. *FP-Agent: Fingerprinting AI Browsing Agents*. 2026 预印本。[arXiv](https://arxiv.org/abs/2605.01247)。读方法、人类样本说明、§6 泛化与闭集限制，未独立复现。

**P4** Choudhary et al. *What Does It Take to Detect an AI Agent? Minimal Feature Sets for Behavioral Detection under Browser Automation*. 2026。[arXiv](https://arxiv.org/abs/2607.26935)。arXiv 记录注明 workshop 接收，未独立核对会议；读摘要、信号解释与限制，不把受控高识别率外推为全网保证。

### 8.4 补充线索与正式系统论文

**P5** Fayolle et al. *On the Internet, Nobody Knows You're an LLM Bot: Unmasking Web Agents with Multi-Layer Fingerprinting*. 2026 预印本。[arXiv](https://arxiv.org/abs/2606.30119)。只读摘要，作为范围线索。

**P6** Cho et al. *Overload Control for µs-scale RPCs with Breakwater*. OSDI 2020, pp.299–314。[USENIX 官方页](https://www.usenix.org/conference/osdi20/presentation/cho)。已读官方摘要，未读完整论文，不引用其数字为网站场景效果。

**P7** Li, X., Azad, B. A., Rahmati, A., & Nikiforakis, N. *Good Bot, Bad Bot: Characterizing Automated Browsing Activity*. IEEE S&P 2021。[DOI](https://doi.org/10.1109/SP40001.2021.00079)。仅 Crossref 元数据核验，全文未读，不用于具体数字论证。

**检索与局限：**AMiner `web bot detection` 请求返回 Invalid Token。转用 arXiv `web bot detection`、`web agent bot detection`、`AI crawler web traffic`，每个查询筛查首屏最多 50 条，排除社交账号 bot 等无关方向；Crossref 进行主题和精确题名检索，未将不匹配结果视为证据。BotGraph 摘要作为历史线索保留，未将其无匹配 Crossref 结果推断为不存在；Raincheck 查询未确认对应题名，未采用。此次是定向补充调研，不是穷尽综述。证据快照和来源清单可供复查；近期预印本和厂商文档的证据角色已分别标注。使用 AI 辅助检索与审查；未对网站实施压测，未实现系统，也没有本课设性能结果。
