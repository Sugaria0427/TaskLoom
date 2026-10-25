# TaskLoom PR #3 五项审查意见整改 · 验收记录

**日期** 2026-10-21 · **分支** `feat/wp2-multi-tool` · **head** `b2efa5e`
**审查基线** head `3b48fdd` / base `efb6d12` · **结论** 完整验收通过，可合并

## 1. 整改范围（对应 Gitee PR #3 审查评论 ID 51687047）

| # | 审查意见 | 处置 |
|---|---|---|
| 2.1 | 最终汇总混用全文统计与搜索匹配统计 | `Checkpoint.completed_tool_calls` 记录轮次化工具步骤键；权威统计只取 `file_stats` 成功结果；无 `file_stats` 返回 `UNVERIFIED_RESULT`；不一致返回 `RESULT_MISMATCH` |
| 2.2 | 未知工具被静默执行为 `file_stats` | 执行器显式分派 `file_stats`/`grep`，其余抛错且不发布结果文件；`ClientService.tool()` 在创建 Attempt 前执行 `ToolRegistry.require()` 与 `ToolSpec.check()`，主机层/执行器层双重失败关闭 |
| 2.3 | 不同工具复用同一 `call_id` 错误复用旧结果 | 步骤身份轮次化 `{task}-model-{round}-tool-{call_id}`；Attempt 保存请求摘要；同身份不同请求返回 `ATTEMPT_CONFLICT`；历史 Attempt 无请求摘要失败关闭 |
| 2.4 | 参数工具可绕过声明的参数约束 | `ToolSpec.check()` 统一结构校验（缺失/额外/类型错误 → `INVALID_TOOL`）；七种顶层 JSON 类型；schema 输出 `required` 与 `additionalProperties: false` |
| 2.5 | 注册表容量与模型请求工具上限不一致 | `MAX_MODEL_TOOLS = 8` 同时约束 `ModelRequest.tools` 与 `ToolRegistry`；超出返回 `TOOL_LIMIT` |

涉及文件：`contracts/models.py`、`runtime/{agent,ports,registry}.py`、`execution/tool.py`、`client/service.py`、`tests/{unit,integration}` 4 个测试文件、README/AGENTS/2 份工程文档。

## 2. 验证证据

### 2.1 Windows 定向（整改代码副本，Python 3.12.14）

| 检查 | 结果 |
|---|---|
| 定向回归 `pytest -q tests/unit tests/integration/test_tool_identity.py` | 29 passed |
| 定向严格类型检查（6 源码 + 3 测试 = 9 文件，`--platform linux`） | 0 errors |
| ruff check / format | All checks passed / 58 files already formatted |
| 分层契约 `lint-imports` | 3 kept, 0 broken |

### 2.2 Linux 虚拟机实测（Ubuntu 20.04，Python 3.12，2026-10-21）

| 检查 | 命令 | 结果 |
|---|---|---|
| 真实执行器定向 | `pytest -q tests/integration/test_executor.py tests/integration/test_tool_identity.py` | **9 passed**（覆盖 Linux subprocess / 真实 ProcessExecutor 路径） |
| 项目门禁 | `tools/project.py check` | ruff 全过；**mypy 37 源文件 0 errors**；lint-imports **3 kept, 0 broken** |
| 全量回归 | `tools/project.py test` | **60 passed**（unit + integration + smoke 全量） |

## 3. 结论

五项 P2 审查意见全部按正确性、测试与文档三层收敛；Linux 真实执行器、cgroup 与项目门禁已在虚拟机实测通过并保留输出。

**PR #3 完整验收通过，具备合并条件。**

## 4. 边界声明

- 本整改未新增产品功能；受控子 Agent、父子依赖、分支预算共享、调度实验负载与 OpenClaw 接入属后续工作包（WP2 STEP 2-4，`feat/wp2-subagent`）
- 虚拟机环境记录：Ubuntu 20.04.6 LTS（早期 `python3.12` 包缺失记录不代表当前环境；本记录以实测输出为准）
