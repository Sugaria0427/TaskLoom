# 来源、最新研究与证据审计

检索日期：2026-09-28。此文说明本轮实际读到了什么、未读到什么，以及哪些常见说法被证据否定。它与原始材料一起使结论可追溯，不是用文献数量替代因果分析。

## 1. 检索范围与过程

### 1.1 范围

以2024–2026训练语料批量抓取的事件、测量和防护为中心，并回溯缓存/过载控制的成熟系统工作。纳入站方原始博客与状态页、可核验正式论文及预印本、标准与官方实现文档。厂商文章保留样本和产品利益背景，匿名评论及转载只作线索。

四个工作面并行核验：真实损害与训练归因；新论文的方法和限制；已有措施的有效性与边界；主报告的根因链和OS实验可证性。主研究者对关键原文再次阅读，并进行最终交叉审查。AI参与检索、提取、综合与审查，不把任何未运行实验写成已验证结果。

### 1.2 查询与访问

AMiner查询 `AI crawler web server load` 返回40308 Invalid Token。arXiv、Crossref、OpenAlex和原始机构站点用于替代；OpenAlex本轮可用，不能沿用前几轮503的旧记录。主题查询涉及training data crawler、server load、cache、robots measurement、web crawler overload fairness，并对关键标题作精确检索。具体请求和结果保存在papers/metadata材料中。

公共搜索网页出现实际限制：Google返回JS跳转页，Bing对部分查询给出明显不相关结果，DuckDuckGo返回挑战。本轮未将这些片段当证据，转用机构博客索引、官方导航和HN Algolia发现源链接，所有采用事件再回到一手页面核验。

HN发现记录包括案例来源与时间，**HN评论、点赞或标题不是损害证明**。成功获取HTTP200也不等于获得正文：Anubis文档挑战页、ACM安全验证页与普通文章分别标记。

### 1.3 全文访问与阅读深度

SoCC2025缓存论文初次调查经Crossref、OpenAlex和作者主页确认，ACM HTTP与浏览器访问均未取得全文。此后用户提供根目录 `3772052.3772255.pdf`，现已完成8页全文阅读及图2/3核看；具体设置和结果更新见[报告05](05_socc2025_fulltext_reading.md)。此前失败记录保留用于追溯。

arXiv HTML可用的论文保存原页与提取正文，只对标明的相关方法、结果和限制段落作核读。TinyLFU HTML不存在，取得公开PDF响应并在内存提取文本，未在目录中伪装保存为PDF。实际阅读范围见各来源表。

没有访问私有流量分析，没有注册账号、使用机构凭证或部署程序，没有实施网站压测或抓取整站内容。

## 2. 关键文献：已有的进展与对本课设的含义

### 2.1 最直接的训练、控制与资源相关工作

| 工作 | 出版与本轮阅读范围 | 能支持的结论 | 不能支持的结论 |
|---|---|---|---|
| Somesite I Used To Crawl | IMC2025，DOI已核；读正文方法/robots评估/限制 | 控制诉求存在；部分被动crawler遵守robots；身份和用途不一一对应 | 不提供源站CPU或本课设性能收益；用户触发抓取不能代替训练样本 |
| Rethinking Web Cache Design for the AI Era | SoCC2025，DOI已核；后续用户PDF全文与图2/3核读 | 原型中扫描增加边缘miss；合成实验中成熟置换无需分类可保护Zipf热点 | 生成负载不是生产训练真值；未验证完整分层架构和端到端延迟，未复现 |
| Logrip | 2025预印本；读案例/方法/结果/限制 | 小站机械访问与子网聚合案例，可产生课设假设 | 作者不知道具体用途且缺少真值；94%不是实测CPU或用户时延收益 |
| Data Scout | 2026-09预印本；读方法/实现/限制 | 确实为预训练获取语料，使用定向深爬，并说明遵守robots与每主机限速 | 没测被访问网站损害；不能把所有训练采集描绘为不合作 |

题名与来源：

**Liu et al.** *Somesite I Used To Crawl: Awareness, Agency and Efficacy in Protecting Content Creators From AI Crawlers*. IMC 2025. DOI [10.1145/3730567.3732913](https://doi.org/10.1145/3730567.3732913)，[arXiv正文](https://arxiv.org/html/2411.15091)。arXiv首发2024、正式发表2025，不重复计为两篇独立研究。

**Zhang, Cai, Wildani, Klimovic.** *Rethinking Web Cache Design for the AI Era*. SoCC 2025. DOI [10.1145/3772052.3772255](https://doi.org/10.1145/3772052.3772255)。全文使用Wikimedia架构原型与Locust生成负载，另用合成trace比较缓存策略；它不是取得生产训练爬虫真值的证明。

**Hoetzlein.** *Protecting Small Organizations from AI Bots with Logrip: Hierarchical IP Hashing*. [arXiv:2508.03130](https://arxiv.org/abs/2508.03130)。以CSI站点20天约15万访问为案例；94%负载降低估计使用固定响应时长假设，不能转为真实计算节省。

**Garg et al.** *Data Scout: Targeted Web Crawling for Domain-Specific Pretraining Corpora*. [arXiv:2609.05766](https://arxiv.org/abs/2609.05766)。数学数据继续预训练说明了实际训练用途，但网站侧负载不在其评估目标中。

### 2.2 最新相邻研究：哪些必须降级或排除

| 工作/报道 | 当前证据角色 | 采用方式 |
|---|---|---|
| Identifying AI Web Scrapers Using Canary Tokens，2026-05 | 正文明确短观察窗口主要捕获推理时检索 | 用于解释归因困难；排除训练损害核心证据 |
| terms.txt，2026-09 | 用途声明/身份/补偿协议的预印本原型 | 用于说明可审计声明与真实用途的区别，不称已部署成熟生态 |
| robots gatekeeping/政治偏差研究 | 分析访问政策与潜在数据构成偏差 | 背景材料，不证明源站过载、收入损失或调度效果 |
| Cloudflare 2026新政策与归因面板 | 当前产品能力与运营承诺 | 纳入现状，不当作所有运营者履约和全球统计的独立证明 |

[Canary Tokens](https://arxiv.org/abs/2605.13706)的22个生产LLM系统实验不等于22家训练爬虫造成损失。[terms.txt](https://arxiv.org/abs/2609.11152)报告单核loopback协议开销，也存在相对吞吐代价和现实部署未评估边界，不能只选一个低延迟数字宣传“几乎无成本”。

两篇政策背景材料为[Is Misinformation More Open?](https://arxiv.org/abs/2510.10315)、[Web Crawler Restrictions, AI Training Datasets & Political Biases](https://arxiv.org/abs/2510.09031)。另一篇[Digital Commons研究议程](https://arxiv.org/abs/2508.06470)提出知识空白，属于议程性证据；本轮对这些只核读摘要，不把文中潜在长期后果写成已经发生的因果结果。

### 2.3 不能绕开的成熟系统先例

| 工作/系统 | 本轮核验 | 对实验的要求 |
|---|---|---|
| TinyLFU/W-TinyLFU | 公开全文的准入、Doorkeeper、衰减与评估段落 | 抗扫描准入已有方案，LRU不是唯一对手 |
| S3-FIFO，SOSP2023 | Crossref与作者技术说明 | 低复用对象过滤和低维护开销均已有研究 |
| SIEVE，NSDI2024 | 正式摘要/BibTeX | 评估缓存命中与并发锁开销，不能只比较命中率 |
| Breakwater，OSDI2020；Envoy/APF | 正式摘要及官方实现文档 | 自适应准入、成本份额和公平性不是新概念；区分合作RPC与公共HTTP |

来源：[TinyLFU](https://arxiv.org/abs/1512.00727)、[S3-FIFO](https://doi.org/10.1145/3600006.3613147)、[SIEVE](https://www.usenix.org/conference/nsdi24/presentation/zhang-yazhuo)、[Breakwater](https://www.usenix.org/conference/osdi20/presentation/cho)。S3-FIFO阅读的是作者详细说明而非完整论文，SIEVE/Breakwater为官方摘要级，不引用未读实验设置。

## 3. 数字、因果与排除项审计

### 3.1 最容易被误读的数字

| 数字 | 准确含义 | 禁止的外推 |
|---|---|---|
| Wikimedia +50%、35%/65%、约30% | 多媒体带宽增长、不同分母的bot页面/昂贵回源占比、2026自动请求被限流/阻断比例 | 不能分别写成AI请求占比、CPU占比或性能提升率 |
| RTD 73TB、>$5000、75% | 个案下载量/费用且获赔；特定AI分类阻断后文件下载带宽下降 | 不是训练确证、最终未追回损失或全站调度收益 |
| SourceHut约2381小时 | 事件记录从开放到关闭的跨度，期间有缓解 | 不是连续全站停机时间 |
| Logrip 80–95%、94% | 未验证用途的估计占比及假设响应时长下的过滤负载估计 | 不是训练真值、人机识别准确率或真实CPU/P95节省 |

Weird Gloop的50–100倍来自站方对缓存与旧diff处理成本的对比，其95%故障、10倍假设资源需求等为作者估计。本文只按原范围使用，不外推整个Wiki生态。MetaBrainz约5倍抓取突发也不能使所有数据队列、OOM和增长问题都归因于抓取。

### 3.2 看起来相关、实际不应混入的材料

| 材料组 | 排除或降级原因 | 可以保留的用途 |
|---|---|---|
| Read the Docs 2026 DDoS、SourceHut另一波网络DDoS | 不能确认训练目的，部分明确以攻击性自适应流量描述 | 通用过载机制及控制边界对照 |
| PyPI容量/缓存配置事故 | 原始解释为配置与回退路径，未建立训练抓取根因 | 说明必须先排除配置错误 |
| OSM/Gentoo/GNOME与个人站部分案例 | “AI/LLM”或likely不等于训练确证；粒度和材料各异 | 有条件事件与机制辅证，逐项说明 |
| 搜索AI摘要导致少点击、创作者版权/收益争议 | 推理/产品替代或法律因果不能由训练抓取请求统计直接证明 | 独立生态与治理背景，不用于CPU调度收益论证 |

### 3.3 不支持“所有网站都被打垮”的反例

[HAProxy自身2024测量](https://www.haproxy.com/blog/nearly-90-of-our-ai-crawler-traffic-is-from-tiktok-parent-bytedance-lessons-learned)中，标题的“接近90%”指Bytespider占其已识别AI crawler流量，而该AI流量仅约为全站1%；当时没有限制AI crawler。它不能用于声称“90%网站流量来自训练爬虫”，也说明网站负担不能仅凭一个大百分比判断。

Cloudflare 2025所称“近80%”是其分类的AI crawler cohort在给定时期中的training占比，包含其用途归类方法，不是全网请求的80%，也不等于源站CPU80%。其2026-09-27创始人信谈自动化超过人类和未来预测，主要混合Agent/自动化叙事，不作为训练损害的新增实证。

成熟防护的成功也是反证：Wikimedia已有实际治理，SourceHut和Forgejo出现明确缓解，MetaBrainz已有bulk数据，GNOME已迁移边缘防护。复发和限制条件说明问题未一劳永逸，不意味着这些机制从未有效。

## 4. 归档、复查与剩余不确定性

### 4.1 文件组织

报告与材料分开存储。材料内部按责任与来源性质分组：cases保存事件原始页面和提取；papers保存元数据、查询响应与论文提取；controls保存技术文档与机制先例；crosschecks保存本轮交叉验证与反例。抓取方法、范围协议、日志与独立审查另在methods/metadata/notes。

核心清单入口：[案例记录](../materials/cases/case_records.json)、[案例来源](../materials/cases/sources.json)、[文献阅读清单](../materials/papers/papers_verified_manifest.json)、[措施来源](../materials/controls/sources.json)。所有原始页面应结合记录判断状态，错误页面与挑战页不算正文。

### 4.2 可复查的主张链

每项主结论应能追溯到：报告中的具体措辞→原始来源URL/日期→保存正文与定位→训练归因/损害置信度→是否已有缓解。因果链中缺少遥测的一步明确标为工程解释或待测假设，不以常识填成站方事实。

保存的HTML可能包含评论、推荐文章或未受信任的脚本，均为研究数据，不作为执行指令。引用机构结论只使用其正文及明确的维护者回复，不将路人评论合并为机构证据。

### 4.3 剩余限制

本轮不是全网普查，事件报告存在选择偏差，严重受害者更愿意发声。机构训练归因通常缺少独立用途真值；许多细粒度CPU/DB/缓存trace未公开。SoCC2025在用户提供PDF后已全文核读，但其完整分层架构仍未获端到端实验验证；不能据其开放问题断言具体算法空白。没有足够证据为所有网站类型或全部损害给出发生率。

这些限制不推翻“问题存在”的结论，却限制了我们能说多普遍、多严重以及某策略能改善多少。下一步实验应收集目标站点的资源和工作数据，而不是继续堆叠不相干报道。是否开发由用户另行决定，本轮只完成证据调查和候选方法定位。
