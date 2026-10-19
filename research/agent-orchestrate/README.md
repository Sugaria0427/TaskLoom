# TaskLoom Agent调度调研资料库

> 报告负责表达当前结论，材料保存原始证据，四层分类与统一编码连接二者。

**正文对齐** 2026-10-20　**材料整理日期** 2026-10-14　**基础证据** 2026-09-29　**OpenClaw补充** 2026-10-14　**当前范围** 个人端L1–L3与受控平台端L4。

TaskLoom协调本地工具执行、Harness任务推进、个人模型请求和平台多用户请求。个人端主线为自研轻量运行时，OpenClaw用于机制与原版产品对照，完整兼容接入后续扩展；当前工程能力见[实现状态](../../docs/engineering/TaskLoom_当前实现与开发状态.md)。模型能力与推理配置固定。基础资料完成四层归类与编码，OpenClaw补充进行了版本、源码和公开问题核查。产品状态依每条来源的日期与版本理解。

阅读顺序为：先看四层主题结论，再看已有能力与接入条件，最后看OS方法及实验。需要核查时，从主题编号进入来源索引和原始证据。

## 1. 三篇当前报告

当前报告区只保留以下三篇，各篇承担独立职责。

| 报告 | 主要内容 | 解决的阅读问题 |
| :--- | :--- | :--- |
| [01 四层痛点与编码主题](reports/01_layered_theme_report.md) | 16个主题、根因、已有覆盖、剩余状态、严重度和证据。 | 问题是否成立，哪些值得进一步验证？ |
| [02 现有方案与落地接入](reports/02_solutions_and_integration.md) | 资源底座、Harness接口、额度控制、平台基线，以及第5章OpenClaw机制、问题与接入。 | 已有什么方案，我们在哪些位置能够介入？ |
| [03 OS方法与验证设计](reports/03_os_design_and_validation.md) | 四层方法、阶段化银行家、生命周期、请求公平与五组对照。 | 如何实现、怎样验收、如何识别真实增量？ |

旧报告中的范围纠正、重复叙述及过时推荐退出当前正文。其原始内容保存在历史快照中供追溯。


---

## 2. 四层材料结构

每个来源只在主层存放一份，原始网页、提取文本和对应评论放在同一来源目录。下表目录为本地证据存储位置；公开仓库通过[来源索引](materials/_index/source_index.md)访问原始网址，证据文件按忽略规则留存。跨层用途通过`related_layers`、`theme_ids`和主题角色记录。

| 主层 | 材料目录 | 管理对象 |
| :--- | :--- | :--- |
| L1 | 本地资源材料（本地留存：`research/agent-orchestrate/materials/L1_local_resources`） | 工具容量、进程树、产物与共享访问。 |
| L2 | Harness任务材料（本地留存：`research/agent-orchestrate/materials/L2_harness_tasks`） | 根任务、子Agent、依赖、状态、结果与恢复。 |
| L3 | 个人请求材料（本地留存：`research/agent-orchestrate/materials/L3_client_requests`） | 个人任务的模型请求、额度、费用与重试。 |
| L4 | 平台请求材料（本地留存：`research/agent-orchestrate/materials/L4_platform_requests`） | 多用户服务份额、接纳、派发与两端反馈。 |

当前目录形态如下，索引与归档是资料管理目录，不增加业务层次。

```text
agent-orchestrate/
├── README.md
├── reports/                         当前三篇报告。
└── materials/
    ├── L1_local_resources/          按来源ID保存本地资源材料。
    ├── L2_harness_tasks/            按来源ID保存任务管理材料。
    ├── L3_client_requests/          按来源ID保存个人请求材料。
    ├── L4_platform_requests/        按来源ID保存平台请求材料。
    ├── _index/                      编码、来源、主题和迁移索引。
    └── _archive/                    历史快照、范围外材料和链接备份。
```


---

## 3. 编码与查询入口

来源编号保持稳定；主题编号采用`T-Lx-nn`。解决状态、证据等级和剩余严重度分开编码；一条来源可以支持多个主题，同源论文和同一机制家族按各自的去重键处理。

查询入口分为四组。

| 入口 | 文件 | 用途 |
| :--- | :--- | :--- |
| 编码规则 | [codebook.md](materials/_index/codebook.md) | 四层含义、字段、状态判定和后续维护方式。 |
| 来源导航 | [source_index.md](materials/_index/source_index.md)、[source_catalog.json](materials/_index/source_catalog.json) | 按来源ID查文本、类型、主层、关联主题及核查记录。 |
| 主题归纳 | [analysis_catalog.json](materials/_index/analysis_catalog.json)、[theme_matrix.csv](materials/_index/theme_matrix.csv) | 按层、状态、证据和严重度筛选16个主题。CSV采用UTF-8 BOM，可直接用于表格筛选。 |
| 正式引用映射 | [citation_map.json](materials/_index/citation_map.json) | 对应项目说明书S01–S23，保留已映射和外部引用项。 |

来源总索引同时提供公开链接与本地定位，保留原有177条记录，并新增OpenClaw 72条，共249条。它们包含论文、文档、问题单、源码和数据记录，数量按来源类型解释。当前尚未保存独立产品复现或TaskLoom性能测量结果。


---

## 4. 归档与完整性

原目录的文件快照保存在整理前快照（本地留存：`research/agent-orchestrate/materials/_archive/before_reorganization_20261014.zip`）。迁移记录逐条保存旧路径、新位置、文件哈希和归档位置，能够追溯全部原始文件。

维护记录包括[迁移清单](materials/_index/migration_manifest.json)、[整理摘要](materials/_index/reorganization_summary.json)与[完整性检查](materials/_index/validation.json)。项目说明书和既有引用文档只同步了受迁移影响的本地链接，原文件另有备份。

新增材料先登记来源身份与主层，再关联主题；更新产品状态时记录核查日期、版本与依据。主题报告承载综合判断，材料保留具体原文，使报告区持续保持三个明确入口。


OpenClaw机制对照与可选接入专题位于[报告02第5章](reports/02_solutions_and_integration.md#5-openclaw对照专题机制剩余问题与可选接入)，检索覆盖、版本与状态记录见[专题核查记录](materials/_index/openclaw_audit.json)。
