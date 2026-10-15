# TaskLoom「织序」文档入口

本目录保存正式说明书、开发路线、工程计划、开题报告、设计图和引用审查。阅读顺序为当前完整说明书、工程搭建计划、系统图示及引用依据；编写工具、素材、构建输出与原始研究材料分别在仓库的对应目录维护。

## 1. 正式文档

| 阅读目的 | 文件 |
|---|---|
| 了解当前完整项目、两端设计、OS 技术和验收。 | [项目说明书：当前完整方案](TaskLoom_项目说明书_当前完整方案.md) |
| 从 Gitee 加入团队、生成成员邀请链接、认领工作包和提交 PR。 | [Gitee 协作说明](engineering/TaskLoom_Gitee协作说明.md) |
| 从零建立本机代码骨架，明确模块、接口、工程约束和验收。 | [工程骨架搭建计划](engineering/TaskLoom_工程骨架搭建计划.md) |
| 了解当前代码结构、运行进程、状态所有权及扩展接口。 | [基础工程架构](engineering/TaskLoom_基础工程架构.md) |
| 复现启动、测试、Linux 保护与独立安装结果。 | [基础工程验收记录](engineering/TaskLoom_基础工程验收记录.md) |
| 了解当前统一系统、账户与固定模型池改造及验证。 | [统一系统改造与验收](engineering/TaskLoom_统一系统改造与验收.md) |
| 了解自研范围、OpenClaw 的角色与对照实验。 | [开发路线与 OpenClaw 对照方案](TaskLoom_开发路线与OpenClaw对照方案.md) |
| 了解银行家算法、死锁控制和优先级继承。 | [OS 算法补充说明](TaskLoom_OS算法补充说明.md) |
| 查阅前版说明与详细技术计划。 | [前版分层说明书](TaskLoom_项目说明书_分层清晰版.md) · [Markdown 计划书](TaskLoom_开题项目计划书.md) · [Word 计划书](TaskLoom_开题项目计划书.docx) |
| 准备答辩及团队讲述。 | [答辩 Q&A](TaskLoom_答辩Q&A.md) · [答辩 PPT](presentations/TaskLoom_答辩修订版/TaskLoom_开题报告_答辩修订版.pptx) · [PDF 预览](presentations/TaskLoom_答辩修订版/TaskLoom_开题报告_答辩修订版.pdf) |

个人端包含 L1 本地资源、L2 Harness 任务管理与 L3 请求管理；平台端提供注册账号、L4 和统一固定模型池。个人端使用本平台账号 API Key，所有模型请求经本平台。模型配置与总服务容量在比较中保持固定，性能指标通过后续实验测量。

2026-10-15 完整说明书、工程架构与搭建计划已对齐 0.2 统一系统。基础工程验收记录保存 0.1 基线，最新结果见统一系统改造记录。前版说明、PPT 和图稿作为历史设计保存，正式展示前按当前账号与服务池模型同步。

## 2. 图稿、引用与审查

| 目录 | 内容 |
|---|---|
| [pictures/](pictures/README.md) | 前版 UML 与思维导图，保留可编辑 draw.io 与导出；展示前按 0.2 账号及模型池关系同步。 |
| [diagrams/](diagrams/README.md) | 前版建模图稿，保留 PlantUML 和可编辑源文件，供追溯使用。 |
| [presentations/](presentations/) | 当前答辩稿、前版 PPT/PDF，以及各自的修改说明和方案。 |
| [references/](references/) | [引用核查](references/final_citation_audit.md)、[选材说明](references/source_selection.md)及来源索引。 |
| [reviews/](reviews/) | [原 PPT 审查意见](reviews/TaskLoom_PPT_20260930/审查意见.md)和计划书技术审查。 |

## 3. 工作区与调研

[Agent 工作区](../agent-workspace/README.md)集中维护编写程序、文档素材、原始输入与检查输出。[Agent 调研入口](../research/agent-orchestrate/README.md)维护四层材料及统一报告；[文档输入材料归档](../research/agent-orchestrate/materials/_archive/documentation_inputs/README.md)保存附件与早期 OS 材料。历史备份统一保存在[backup](../backup/)中。

## 4. 使用与维护

修改说明书时维护 Markdown 正文；修改正式图稿时使用对应的可编辑源文件。运行生成程序前，确认工作区说明及输出位置。当前分层说明书独立维护，原计划书的构建程序负责技术版计划书。
