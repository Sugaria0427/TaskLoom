# TaskLoom 四层来源索引

> 来源编号保持稳定，主层确定材料位置，跨层用途由主题角色表达。

**整理日期** 2026-10-14　**基础证据** 2026-09-29　**OpenClaw补充** 2026-10-14　**记录数** 249。

原有177条记录保留，新增OpenClaw 72条文档、源码、问题与PR来源。source_id标识来源，canonical_key用于同源去重，mechanism_family用于合并相关机制叙述。

案例状态按“解决程度/剩余严重度/证据等级”显示，版本差异见source_catalog.json中的version_status。

主题筛选见[theme_matrix.csv](theme_matrix.csv)，规则见[codebook.md](codebook.md)，OpenClaw覆盖与版本见[openclaw_audit.json](openclaw_audit.json)。

## 1. L1：本地资源与工具执行

每个来源在主层只保存一份，关联主题包括案例支持、现有控制和反证。

| 来源ID | 主题关联 | 标题 | 范围／案例编码 | 证据文件 |
| :--- | :--- | :--- | :--- | :--- |
| P01 | 资料与版本背景 | Bash tool: runaway child processes are memory-uncapped and outlive their parent shell's timeout — parallel sub-agents' orphaned pytest children exhausted 128 GB RAM and caused a macOS watchdog kernel panic | CORE · U/NA/E3 | [阅读归档](../L1_local_resources/P01/P01_issue.txt) |
| P02 | 资料与版本背景 | [BUG] JavaScript heap out of memory crash when running parallel Explore subagents | CORE · U/NA/E3 | [阅读归档](../L1_local_resources/P02/P02_issue.txt) |
| P03 | 资料与版本背景 | [BUG] Windows: timed-out git status leaves orphaned git.exe processes that pile up until memory is exhausted | CORE · P/M/E3 | [阅读归档](../L1_local_resources/P03/P03_issue.txt) |
| P04 | T-L1-04 | Second stdio client in the same workspace hits persistent profile lock on first browser action | CORE · C/NA/E1 | [阅读归档](../L1_local_resources/P04/P04_issue.txt) |
| P05 | 资料与版本背景 | [Bug] Running multiple parallel async browsers in --isolated mode leaves orphan browsers. This has started happening after Release v0.0.69. | CORE · U/NA/E3 | [阅读归档](../L1_local_resources/P05/P05_issue.txt) |
| P06 | T-L1-02 | mcp: orphaned local MCP server processes accumulate after sessions end | CORE · P/M/E3 | [阅读归档](../L1_local_resources/P06/P06_issue.txt) |
| P07 | T-L1-04 | File cache conflicts when running multiple Claude Code instances with git worktrees | CORE · U/NA/E3 | [阅读归档](../L1_local_resources/P07/P07_issue.txt) |
| P10 | T-L1-04 | Playwright MCP README | CORE | [阅读归档](../L1_local_resources/P10/P10_text.txt) |
| P13 | 资料与版本背景 | Terminate whole MCP process tree PR50509 | CORE · P/NA/E3 | [阅读归档](../L1_local_resources/P13/P13_raw.txt) |
| P17 | T-L1-01 | PeakBench | CORE | [阅读归档](../L1_local_resources/P17/P17_text.txt) |
| L07 | T-L1-01 | PeakBench: Benchmarking Resource-AwareTool Invocation in LLM Agents | CORE | [阅读归档](../L1_local_resources/L07/L07.txt) |
| CX26984 | T-L1-02 | MCP stdio servers leak pipe fds + orphan child processes → cumulative EMFILE ("Too many open files", os error 24) | CORE · U/NA/E3 | [阅读归档](../L1_local_resources/CX26984/26984.txt) |
| CX34473 | 资料与版本背景 | Codex App (Windows): an unresolvable git root causes an unbounded, no-backoff retry loop that saturates the entire machine — 142/168 git calls hit the 60s timeout | CORE · U/NA/E3 | [阅读归档](../L1_local_resources/CX34473/34473.txt) |
| CX34953 | T-L1-03 | Windows Codex Desktop repeatedly leaks full-size Git LFS temp copies while refreshing a repo | CORE · P/H/E3 | [阅读归档](../L1_local_resources/CX34953/34953.txt) |
| CX48120 | 资料与版本背景 | Codex CLI 0.157.0 spawns blank Windows Terminal windows during sandbox setup refresh | ADJACENT · P/NA/E3 | [阅读归档](../L1_local_resources/CX48120/48120.txt) |
| CX48524 | T-L1-02 | app-server: stdin EOF SIGKILLs stdio MCP servers 5–10 ms after SIGTERM, before they can clean up | CORE · P/H/E3 | [阅读归档](../L1_local_resources/CX48524/48524.txt) |
| CX48543 | T-L1-03 | macOS app-server: pipe/child growth with unchanged session count; network revocation hides policy-load errors | CORE · U/NA/E3 | [阅读归档](../L1_local_resources/CX48543/48543.txt) |
| CX12647 | T-L1-02 | Avoid AbsolutePathBuf::parent panic under EMFILE | CORE · D/NA/E1 | [阅读归档](../L1_local_resources/CX12647/12647.txt) |
| CX30408 | 资料与版本背景 | 30408 | CORE · U/NA/E3 | [阅读归档](../L1_local_resources/CX30408/30408_web.txt) |
| CX48074 | 资料与版本背景 | 48074 | ADJACENT · U/NA/E3 | [阅读归档](../L1_local_resources/CX48074/48074_web.txt) |
| CX-SOURCE | T-L1-02 | rust-v0.158.0 | CORE | [阅读归档](../L1_local_resources/CX-SOURCE/source_residency.txt) |
| CC10 | 资料与版本背景 | Monitor/TaskStop removes the task from the panel but leaves the spawned process running (orphaned tail -F accumulates) · Issue #95242 · anthropics/claude-code · GitHub | CORE · U/NA/E3 | [阅读归档](../L1_local_resources/CC10/issue_95242.txt) |
| CC14 | T-L1-03 | [BUG] Clean agent worktrees are never auto-cleaned when a worktree-isolated subagent finishes (2.1.278) · Issue #95644 · anthropics/claude-code · GitHub | CORE · P/M/E3 | [阅读归档](../L1_local_resources/CC14/issue_95644.txt) |
| D5995 | 资料与版本背景 | harness 进程内存随会话事件数持续增长，长批次约 8 小时后堆触顶 OOM 退出（附测量与采样归因） · deepseek-ai/deepseek-harness · Discussion #5995 · GitHub | CORE · U/NA/E3 | [阅读归档](../L1_local_resources/D5995/D5995.txt) |
| D3590 | T-L1-04 | [Bug] Python SDK: concurrent start() spawns duplicate runtimes — orphaned subprocesses survive close() · deepseek-ai/deepseek-harness · Discussion #3590 · GitHub | CORE · P/M/E2 | [阅读归档](../L1_local_resources/D3590/D3590.txt) |
| D7158 | 资料与版本背景 | Writer-lock timeout error names only the path, leaving an orphan lock unrecoverable in practice · deepseek-ai/deepseek-harness · Discussion #7158 · GitHub | CORE · U/NA/E3 | [阅读归档](../L1_local_resources/D7158/D7158.txt) |
| DS_python_client | T-L1-04 | python/sdk/src/deepseek_harness/client.py | CORE | [阅读归档](../L1_local_resources/DS_python_client/source_python_client.txt) |
| DS_python_api | T-L1-04 | python/sdk/src/deepseek_harness/api.py | CORE | [阅读归档](../L1_local_resources/DS_python_api/source_python_api.txt) |
| R07 | T-L4-02、T-L4-04 | Tool invocation scheduling and parallelism contract | CORE | [阅读归档](../L1_local_resources/R07/R07.txt) |
| C01 | T-L1-01 | Resources — Ray 2.58.0 | METHOD | [阅读归档](../L1_local_resources/C01/C01.txt) |
| C02 | 资料与版本背景 | Out-Of-Memory Prevention — Ray 2.58.0 | METHOD | [阅读归档](../L1_local_resources/C02/C02.txt) |
| C03 | T-L1-01 | Control Group v2 — The Linux Kernel documentation | METHOD | [阅读归档](../L1_local_resources/C03/C03.txt) |
| C09 | T-L1-01 | Pattern: Using resources to limit the number of concurrently running tasks — Ray 2.58.0 | METHOD | [阅读归档](../L1_local_resources/C09/C09.txt) |
| C11 | 资料与版本背景 | Job Objects - Win32 apps ／ Microsoft Learn | METHOD | [阅读归档](../L1_local_resources/C11/C11.txt) |
| C12 | T-L1-03 | Subprocesses — Python 3.14.7 documentation | METHOD | [阅读归档](../L1_local_resources/C12/C12.txt) |
| C15 | 资料与版本背景 | Resource Isolation With Cgroup v2 — Ray 2.58.0 | METHOD | [阅读归档](../L1_local_resources/C15/C15.txt) |
| OS21 | T-L1-04 | Dijkstra：银行家算法的数学条件 | METHOD | [阅读归档](../L1_local_resources/OS21/S21_bankers_algorithm.txt) |
| OS22 | T-L1-04、T-L2-01 | UIC：死锁、银行家算法与预防 | METHOD | [阅读归档](../L1_local_resources/OS22/S22_deadlocks_course.txt) |
| OS23 | T-L1-04 | Linux RT-mutex：优先级继承 | METHOD | [阅读归档](../L1_local_resources/OS23/S23_priority_inheritance.txt) |
| OS24 | T-L1-04 | Linux Wound/Wait互斥 | METHOD | [阅读归档](../L1_local_resources/OS24/S24_ww_mutex.txt) |
| OC-D13 | T-L1-01、T-L1-02、T-L1-04 | OpenClaw：docs/gateway/config-agents/sandbox.md | CORE | [阅读归档](../L1_local_resources/OC-D13/sandbox.md) |
| OC-D14 | T-L1-01、T-L1-02 | OpenClaw：docs/gateway/sandboxing/docker-backend.md | CORE | [阅读归档](../L1_local_resources/OC-D14/docker-backend.md) |
| OC-D19 | T-L1-01、T-L1-04、T-L2-03 | OpenClaw：docs/plugins/hooks/tool-policy.md | CORE | [阅读归档](../L1_local_resources/OC-D19/tool-policy.md) |
| OC-D23 | T-L1-02、T-L1-03 | OpenClaw：docs/tools/exec.md | CORE | [阅读归档](../L1_local_resources/OC-D23/exec.md) |
| OC-S07 | T-L1-01、T-L1-04、T-L2-03 | OpenClaw：src/agents/agent-tools.before-tool-call.policy.ts | CORE | [阅读归档](../L1_local_resources/OC-S07/agent-tools.before-tool-call.policy.ts) |
| OC-S09 | T-L1-02 | OpenClaw：src/process/supervisor/supervisor.ts | CORE | [阅读归档](../L1_local_resources/OC-S09/supervisor.ts) |
| OC-S10 | T-L1-02、T-L1-03 | OpenClaw：src/agents/bash-process-registry.ts | CORE | [阅读归档](../L1_local_resources/OC-S10/bash-process-registry.ts) |
| OC-S11 | T-L1-01、T-L1-04 | OpenClaw：src/agents/agent-bundle-mcp-runtime.ts | CORE | [阅读归档](../L1_local_resources/OC-S11/agent-bundle-mcp-runtime.ts) |
| OC-S12 | T-L1-01、T-L1-04 | OpenClaw：src/agents/agent-bundle-mcp-manager.ts | CORE | [阅读归档](../L1_local_resources/OC-S12/agent-bundle-mcp-manager.ts) |
| OC-S19 | T-L1-01、T-L2-03 | OpenClaw：src/plugins/hook-before-tool-call-result.ts | CORE | [阅读归档](../L1_local_resources/OC-S19/hook-before-tool-call-result.ts) |
| OC-S20 | T-L1-02 | OpenClaw：src/process/scoped-child-reaper.ts | CORE | [阅读归档](../L1_local_resources/OC-S20/scoped-child-reaper.ts) |
| OC-S21 | T-L1-02 | OpenClaw：src/process/supervisor/linux-child-subreaper.ts | CORE | [阅读归档](../L1_local_resources/OC-S21/linux-child-subreaper.ts) |
| OC-S24 | T-L1-01、T-L1-04 | OpenClaw：src/agents/agent-bundle-mcp-runtime-shared.ts | CORE | [阅读归档](../L1_local_resources/OC-S24/agent-bundle-mcp-runtime-shared.ts) |
| OC-I161050 | T-L1-01 | [Feature]: memory-pressure-aware admission for sessions_spawn / isolated cron runs | CORE · T/H/E3 | [阅读归档](../L1_local_resources/OC-I161050/issue_and_comments.md) |
| OC-I95506 | T-L1-01、T-L1-04 | MCP runtimes duplicate stdio sessions for identical workspace/config | CORE · T/M/E2 | [阅读归档](../L1_local_resources/OC-I95506/issue_and_comments.md) |
| OC-I65983 | T-L1-02 | Background PTY exec runs can survive restart/session loss and become untracked orphan process trees | CORE · U/NA/E3 | [阅读归档](../L1_local_resources/OC-I65983/issue_and_comments.md) |
| OC-I164294 | T-L1-03 | Gateway state DB WAL auto-checkpoint never completes (pinned by in-process reader) | CORE · U/NA/E3 | [阅读归档](../L1_local_resources/OC-I164294/issue_and_comments.md) |
| OC-I97616 | T-L1-02 | [Bug]: OpenClaw leaks unreaped hook/tool child processes, causing zombie accumulation and runtime degradation | CORE · D/NA/E2 | [阅读归档](../L1_local_resources/OC-I97616/issue_and_comments.md) |

---

## 2. L2：Harness与Agent任务管理

每个来源在主层只保存一份，关联主题包括案例支持、现有控制和反证。

| 来源ID | 主题关联 | 标题 | 范围／案例编码 | 证据文件 |
| :--- | :--- | :--- | :--- | :--- |
| P08 | 资料与版本背景 | Claude Code agent teams | CORE | [阅读归档](../L2_harness_tasks/P08/P08_text.txt) |
| P09 | 资料与版本背景 | Claude Code subagents | CORE | [阅读归档](../L2_harness_tasks/P09/P09_text.txt) |
| P11 | T-L2-04 | LangGraph Persistence | CORE | [阅读归档](../L2_harness_tasks/P11/P11_text.txt) |
| P14 | T-L4-04 | TraceLab: Characterizing Coding Agent Workloads for LLM Serving | CORE | [阅读归档](../L2_harness_tasks/P14/P15_text.txt) |
| P16 | T-L1-01、T-L4-04 | Agentic Coding in the Wild | CORE | [阅读归档](../L2_harness_tasks/P16/P16_text.txt) |
| P18 | T-L1-01 | AIOS: LLM Agent Operating System | CORE | [阅读归档](../L2_harness_tasks/P18/P18_text.txt) |
| P20 | 资料与版本背景 | TraceLab released dataset README | CORE | [阅读归档](../L2_harness_tasks/P20/P20_raw.txt) |
| P24 | T-L2-04 | LangGraph Checkpointers | CORE | [阅读归档](../L2_harness_tasks/P24/P24_text.txt) |
| A10 | T-L2-04、T-L4-02 | Persistence - Docs by LangChain | CORE | [阅读归档](../L2_harness_tasks/A10/A10.txt) |
| A11 | 资料与版本背景 | Task Queues ／ Temporal Documentation | CORE | [阅读归档](../L2_harness_tasks/A11/A11.txt) |
| A20 | T-L3-04 | [Bug]: max_iterations and max_budget_per_session are shared by every agent in the same trace | CORE · U/NA/E3 | [阅读归档](../L2_harness_tasks/A20/A20.txt) |
| A28 | 资料与版本背景 | GitHub issue/PR discussion | CORE · U/NA/E3 | [阅读归档](../L2_harness_tasks/A28/A28.txt) |
| CX38385 | T-L2-02 | [Windows Desktop][26.803.10989.0] Six stuck app-server requests freeze every window and remove Fast mode | CORE · U/NA/E3 | [阅读归档](../L2_harness_tasks/CX38385/38385.txt) |
| CX47855 | 资料与版本背景 | Windows Codex Desktop: second message hangs indefinitely while first message works | CORE · U/NA/E3 | [阅读归档](../L2_harness_tasks/CX47855/47855.txt) |
| CX49027 | T-L2-02 | Windows app: delivered and answered prompts remain queued, blocking subsequent prompts until stale entry is deleted | CORE · P/M/E3 | [阅读归档](../L2_harness_tasks/CX49027/49027.txt) |
| CX49131 | T-L2-03 | IDE extension leaves interrupted tool card stuck on ‘Running’ after process has ended | CORE · P/L/E3 | [阅读归档](../L2_harness_tasks/CX49131/49131.txt) |
| CX-DOC | T-L2-02、T-L3-04 | multi-agent | CORE | [阅读归档](../L2_harness_tasks/CX-DOC/official_multiagent.txt) |
| CX-RELEASE | 资料与版本背景 | rust-v0.158.0 | CORE | [阅读归档](../L2_harness_tasks/CX-RELEASE/releases_web.txt) |
| CC02 | T-L2-04 | Rate limits hard-kill in-flight agent runs, and the model has no budget visibility to avoid it · Issue #94577 · anthropics/claude-code · GitHub | ADJACENT · P/M/E3 | [阅读归档](../L2_harness_tasks/CC02/issue_94577.txt) |
| CC03 | T-L2-04 | [FEATURE] Mark a subagent killed by a usage limit as resumable, and keep it listed after a session restart · Issue #94222 · anthropics/claude-code · GitHub | ADJACENT · P/M/E3 | [阅读归档](../L2_harness_tasks/CC03/issue_94222.txt) |
| CC04 | T-L2-04 | Background subagents burn the weekly quota with no cost visibility, and a rate-limited subagent leaves half-finished work behind · Issue #93911 · anthropics/claude-code · GitHub | ADJACENT · U/NA/E3 | [阅读归档](../L2_harness_tasks/CC04/issue_93911.txt) |
| CC06 | T-L2-02 | Scheduled-task sessions intermittently freeze permanently on first/second tool call, never releasing their concurrency slot · Issue #92753 · anthropics/claude-code · GitHub | CORE · U/NA/E3 | [阅读归档](../L2_harness_tasks/CC06/issue_92753.txt) |
| CC07 | 资料与版本背景 | Scheduled task sessions hang indefinitely (isRunning stuck true, zero progress, no error) - two confirmed triggers fixed, hang persists · Issue #94563 · anthropics/claude-code · GitHub | CORE · U/NA/E3 | [阅读归档](../L2_harness_tasks/CC07/issue_94563.txt) |
| CC08 | T-L2-02 | [BUG] Windows: two Bash tool calls never return and never time out — main session deadlocks for 53 min; agent completions and user input queue behind the dead turn · Issue #91648 · anthropics/claude-code · GitHub | CORE · U/NA/E3 | [阅读归档](../L2_harness_tasks/CC08/issue_91648.txt) |
| CC09 | T-L2-03 | Stopping a background agent does not stop the agents it spawned, and neither the stop result nor the task-notification mentions them · Issue #95138 · anthropics/claude-code · GitHub | CORE · U/NA/E3 | [阅读归档](../L2_harness_tasks/CC09/issue_95138.txt) |
| CC11 | T-L2-03 | SubagentStop does not fire when a background subagent is killed via TaskStop or by "Exit and stop tasks" · Issue #92716 · anthropics/claude-code · GitHub | CORE · U/NA/E3 | [阅读归档](../L2_harness_tasks/CC11/issue_92716.txt) |
| CC12 | T-L2-03 | [BUG] StopFailure fires for internal helper queries and subagent failures on the parent session; under low-priority mode every excluded helper 429 reports a failed turn that never happened · Issue #91419 · anthropics/claude-code · GitHub | CORE · P/M/E3 | [阅读归档](../L2_harness_tasks/CC12/issue_91419.txt) |
| CC13 | 资料与版本背景 | Subagent orchestration has no concurrency model: no join, no cancellation semantics, no quiescent stop, no signal ordering — and the semantics change silently between releases · Issue #87874 · anthropics/claude-code · GitHub | ADJACENT · U/NA/E3 | [阅读归档](../L2_harness_tasks/CC13/issue_87874.txt) |
| CCD01 | T-L2-02、T-L2-04、T-L3-04 | sub-agents | CORE | [阅读归档](../L2_harness_tasks/CCD01/D01.txt) |
| CCD02 | T-L1-04 | agent-teams | CORE | [阅读归档](../L2_harness_tasks/CCD02/D02.txt) |
| CCD04 | 资料与版本背景 | settings | CORE | [阅读归档](../L2_harness_tasks/CCD04/D04.txt) |
| CCD05 | T-L2-03 | hooks | CORE | [阅读归档](../L2_harness_tasks/CCD05/D05.txt) |
| CCD06 | T-L1-02、T-L1-03、T-L2-02、T-L2-03 | CHANGELOG.md | CORE | [阅读归档](../L2_harness_tasks/CCD06/D06.txt) |
| CCD08 | T-L2-04 | settings-reference | ADJACENT | [阅读归档](../L2_harness_tasks/CCD08/D08.txt) |
| D1797 | 资料与版本背景 | Welcome to DeepSeek Harness Community - Please Read Before Posting · deepseek-ai/deepseek-harness · Discussion #1797 · GitHub | CORE | [阅读归档](../L2_harness_tasks/D1797/D1797.txt) |
| D1316 | T-L2-02 | Bug 报告：Web 界面卡顿——后台会话高速输出时新建会话等交互延迟数秒（mux 事件流无订阅过滤、无背压） · deepseek-ai/deepseek-harness · Discussion #1316 · GitHub | CORE · P/M/E3 | [阅读归档](../L2_harness_tasks/D1316/D1316.txt) |
| D4109 | T-L2-03 | Bug: job disposal awaits producer cancellation without deadline — one stuck cancel() stalls whole-process shutdown forever · deepseek-ai/deepseek-harness · Discussion #4109 · GitHub | CORE · U/NA/E3 | [阅读归档](../L2_harness_tasks/D4109/D4109.txt) |
| D4178 | T-L2-01 | [BUG] 两个 dsh web 实例并发打开同一会话，导致会话日志 seq 冲突、历史记录损坏 · deepseek-ai/deepseek-harness · Discussion #4178 · GitHub | CORE · D/NA/E2 | [阅读归档](../L2_harness_tasks/D4178/D4178.txt) |
| D5460 | T-L2-01 | Bug: JSONL session ownership is process-local, allowing concurrent writers · deepseek-ai/deepseek-harness · Discussion #5460 · GitHub | CORE · D/NA/E2 | [阅读归档](../L2_harness_tasks/D5460/D5460.txt) |
| D7936 | T-L2-01 | [Bug] Rejected sibling messages leave cyclic ownership holds and suppress subagent settlement reports (0.1.7-rc.2) · deepseek-ai/deepseek-harness · Discussion #7936 · GitHub | CORE · P/H/E2 | [阅读归档](../L2_harness_tasks/D7936/D7936.txt) |
| D7098 | T-L3-04 | Host setting subagent.maxDepth is not enforced on delegation paths that call SubagentRuntime directly (workflow / spawn_teammate / ralph) · deepseek-ai/deepseek-harness · Discussion #7098 · GitHub | CORE · P/M/E3 | [阅读归档](../L2_harness_tasks/D7098/D7098.txt) |
| D274 | T-L2-02 | [Bug] Follow-up can remain indefinitely queued during driver retirement · deepseek-ai/deepseek-harness · Discussion #274 · GitHub | CORE · P/M/E3 | [阅读归档](../L2_harness_tasks/D274/D274.txt) |
| D4952 | T-L2-02 | Follow-ups queued during an active turn are never admitted after the turn ends (non-cancel wake-latch variant) · deepseek-ai/deepseek-harness · Discussion #4952 · GitHub | CORE · P/M/E3 | [阅读归档](../L2_harness_tasks/D4952/D4952.txt) |
| DS_continuation | T-L2-01 | packages/subagent/subagent/src/continuation.ts | CORE | [阅读归档](../L2_harness_tasks/DS_continuation/source_continuation.txt) |
| DS_activation | T-L2-01 | packages/subagent/subagent/src/continuation-activation.ts | CORE | [阅读归档](../L2_harness_tasks/DS_activation/source_activation.txt) |
| DS_agent_loop | 资料与版本背景 | packages/core/agent-loop/src/agent.ts | CORE | [阅读归档](../L2_harness_tasks/DS_agent_loop/source_agent_loop.txt) |
| DS_session_storage | 资料与版本背景 | packages/session/session-persistence-jsonl/src/storage.ts | CORE | [阅读归档](../L2_harness_tasks/DS_session_storage/source_session_storage.txt) |
| DS_session_index | 资料与版本背景 | packages/session/session-persistence-jsonl/src/index.ts | CORE | [阅读归档](../L2_harness_tasks/DS_session_index/source_session_index.txt) |
| DS_session_manager | 资料与版本背景 | packages/api/session-controller/src/client/sessions/manager.ts | CORE | [阅读归档](../L2_harness_tasks/DS_session_manager/source_session_manager.txt) |
| DS_subagent_docs | T-L2-03 | docs/subsystems/subagent.md | CORE | [阅读归档](../L2_harness_tasks/DS_subagent_docs/source_subagent_docs.txt) |
| DS_lease | T-L2-01 | lease.ts | CORE | [阅读归档](../L2_harness_tasks/DS_lease/source_lease.txt) |
| DS_repo | 资料与版本背景 | deepseek-harness | CORE | [阅读归档](../L2_harness_tasks/DS_repo/repository_page.txt) |
| DS_contributing | 资料与版本背景 | CONTRIBUTING.md | CORE | [阅读归档](../L2_harness_tasks/DS_contributing/official_CONTRIBUTING.md) |
| DS_issues | 资料与版本背景 | issues | CORE | [阅读归档](../L2_harness_tasks/DS_issues/issues_page.txt) |
| R02 | T-L4-02 | Function calling ／ OpenAI API | CORE | [阅读归档](../L2_harness_tasks/R02/R02.txt) |
| R03 | 资料与版本背景 | Intercept and control agent behavior with hooks - Claude Code Docs | CORE | [阅读归档](../L2_harness_tasks/R03/R03.txt) |
| R04 | T-L4-02、T-L4-04 | Custom middleware - Docs by LangChain | CORE | [阅读归档](../L2_harness_tasks/R04/R04.txt) |
| R05 | 资料与版本背景 | Codex App Server ／ ChatGPT Learn | CORE | [阅读归档](../L2_harness_tasks/R05/R05.txt) |
| C05 | T-L3-04 | How we built our multi-agent research system \ Anthropic | METHOD | [阅读归档](../L2_harness_tasks/C05/C05.txt) |
| C06 | 资料与版本背景 | Scaling long-running autonomous coding · Cursor | METHOD | [阅读归档](../L2_harness_tasks/C06/C06.txt) |
| C07 | T-L2-04 | Effective harnesses for long-running agents \ Anthropic | METHOD | [阅读归档](../L2_harness_tasks/C07/C07.txt) |
| C14 | T-L1-04 | Nested Remote Functions — Ray 2.58.0 | METHOD | [阅读归档](../L2_harness_tasks/C14/C14.txt) |
| OC-M01 | 资料与版本背景 | OpenClaw版本、发布、许可与运行环境快照 | CORE | [阅读归档](../L2_harness_tasks/OC-M01/pin.json) |
| OC-D01 | T-L2-02、T-L2-03、T-L2-04 | OpenClaw：docs/concepts/architecture.md | CORE | [阅读归档](../L2_harness_tasks/OC-D01/architecture.md) |
| OC-D02 | T-L2-01、T-L2-02、T-L2-03、T-L2-04 | OpenClaw：docs/concepts/agent-loop.md | CORE | [阅读归档](../L2_harness_tasks/OC-D02/agent-loop.md) |
| OC-D03 | T-L2-04、T-L3-01 | OpenClaw：docs/concepts/agent-runtimes.md | CORE | [阅读归档](../L2_harness_tasks/OC-D03/agent-runtimes.md) |
| OC-D04 | T-L2-04 | OpenClaw：docs/concepts/agent.md | CORE | [阅读归档](../L2_harness_tasks/OC-D04/agent.md) |
| OC-D05 | T-L2-03、T-L3-04 | OpenClaw：docs/concepts/multi-agent.md | CORE | [阅读归档](../L2_harness_tasks/OC-D05/multi-agent.md) |
| OC-D06 | T-L2-02、T-L3-04 | OpenClaw：docs/concepts/queue.md | CORE | [阅读归档](../L2_harness_tasks/OC-D06/queue.md) |
| OC-D07 | T-L2-02、T-L2-03、T-L2-04 | OpenClaw：docs/concepts/subagent-yield-handoff.md | CORE | [阅读归档](../L2_harness_tasks/OC-D07/subagent-yield-handoff.md) |
| OC-D08 | T-L2-02、T-L3-04 | OpenClaw：docs/tools/subagents.md | CORE | [阅读归档](../L2_harness_tasks/OC-D08/subagents.md) |
| OC-D09 | T-L2-02、T-L3-04 | OpenClaw：docs/tools/subagents/nesting.md | CORE | [阅读归档](../L2_harness_tasks/OC-D09/nesting.md) |
| OC-D10 | T-L2-03、T-L2-04 | OpenClaw：docs/tools/subagents/operations.md | CORE | [阅读归档](../L2_harness_tasks/OC-D10/operations.md) |
| OC-D11 | T-L2-02、T-L2-03 | OpenClaw：docs/tools/subagents/tool-reference.md | CORE | [阅读归档](../L2_harness_tasks/OC-D11/tool-reference.md) |
| OC-D12 | T-L2-02、T-L3-04 | OpenClaw：docs/gateway/config-tools/sessions-and-subagents.md | CORE | [阅读归档](../L2_harness_tasks/OC-D12/sessions-and-subagents.md) |
| OC-D18 | T-L1-01、T-L2-03、T-L3-03 | OpenClaw：docs/plugins/hooks/reference.md | CORE | [阅读归档](../L2_harness_tasks/OC-D18/reference.md) |
| OC-D20 | T-L2-03 | OpenClaw：docs/plugins/hooks/lifecycle.md | CORE | [阅读归档](../L2_harness_tasks/OC-D20/lifecycle.md) |
| OC-D22 | T-L2-03 | OpenClaw：docs/plugins/sdk-runtime.md | CORE | [阅读归档](../L2_harness_tasks/OC-D22/sdk-runtime.md) |
| OC-D24 | T-L2-02 | OpenClaw：docs/gateway/protocol/transport.md | CORE | [阅读归档](../L2_harness_tasks/OC-D24/transport.md) |
| OC-S01 | T-L2-02 | OpenClaw：src/process/command-queue.ts | CORE | [阅读归档](../L2_harness_tasks/OC-S01/command-queue.ts) |
| OC-S02 | T-L2-02、T-L3-04 | OpenClaw：src/process/command-queue.types.ts | CORE | [阅读归档](../L2_harness_tasks/OC-S02/command-queue.types.ts) |
| OC-S03 | T-L2-02 | OpenClaw：src/process/command-queue.capacity-groups.ts | CORE | [阅读归档](../L2_harness_tasks/OC-S03/command-queue.capacity-groups.ts) |
| OC-S04 | T-L2-02、T-L3-04 | OpenClaw：src/process/command-queue.state.ts | CORE | [阅读归档](../L2_harness_tasks/OC-S04/command-queue.state.ts) |
| OC-S05 | T-L2-03 | OpenClaw：src/plugins/hooks.ts | CORE | [阅读归档](../L2_harness_tasks/OC-S05/hooks.ts) |
| OC-S06 | T-L2-03 | OpenClaw：src/plugins/hook-types.ts | CORE | [阅读归档](../L2_harness_tasks/OC-S06/hook-types.ts) |
| OC-S08 | T-L2-02、T-L2-03 | OpenClaw：src/agents/embedded-agent-runner/run/lane-controller.ts | CORE | [阅读归档](../L2_harness_tasks/OC-S08/lane-controller.ts) |
| OC-S15 | T-L2-03 | OpenClaw：src/gateway/server-plugins.ts | CORE | [阅读归档](../L2_harness_tasks/OC-S15/server-plugins.ts) |
| OC-S16 | T-L2-03 | OpenClaw：src/gateway/server-plugin-subagent-runtime.ts | CORE | [阅读归档](../L2_harness_tasks/OC-S16/server-plugin-subagent-runtime.ts) |
| OC-S17 | T-L2-02 | OpenClaw：src/gateway/server/ws-connection/request-start.ts | CORE | [阅读归档](../L2_harness_tasks/OC-S17/request-start.ts) |
| OC-S18 | T-L2-03 | OpenClaw：src/plugins/hook-agent-context.types.ts | CORE | [阅读归档](../L2_harness_tasks/OC-S18/hook-agent-context.types.ts) |
| OC-S22 | T-L2-02 | OpenClaw：src/agents/embedded-agent-runner/run/lane-runtime.ts | CORE | [阅读归档](../L2_harness_tasks/OC-S22/lane-runtime.ts) |
| OC-S23 | T-L2-03、T-L3-04 | OpenClaw：src/agents/subagents/spawn/subagent-spawn-lifecycle.ts | CORE | [阅读归档](../L2_harness_tasks/OC-S23/subagent-spawn-lifecycle.ts) |
| OC-I79589 | T-L2-02、T-L3-04 | Command queue has no priority support; latency-sensitive work blocked by background tasks | CORE · D/NA/E1 | [阅读归档](../L2_harness_tasks/OC-I79589/issue_and_comments.md) |
| OC-I54488 | T-L2-02 | Session lane starvation: followup drain monopolizes session lane, blocks inbound dispatch for 20-30min | CORE · U/NA/E3 | [阅读归档](../L2_harness_tasks/OC-I54488/issue_and_comments.md) |
| OC-I161506 | T-L2-02、T-L2-03 | Session lane task stuck active (activeAhead=1 activeNow=0) starves queued turns; scheduled runs burn their wall clock with zero dispatch | CORE · U/NA/E3 | [阅读归档](../L2_harness_tasks/OC-I161506/issue_and_comments.md) |
| OC-I165950 | T-L2-02 | [Bug]: health and status wait behind the request-start preparation queue | CORE · P/M/E2 | [阅读归档](../L2_harness_tasks/OC-I165950/issue_and_comments.md) |
| OC-I171366 | T-L2-02、T-L2-03 | Stuck-session recovery aborts a run whose Code Mode exec is legitimately waiting (awaitResults) on nested sessions_spawn children | CORE · D/NA/E2 | [阅读归档](../L2_harness_tasks/OC-I171366/issue_and_comments.md) |
| OC-I155119 | T-L2-03、T-L2-04 | Subagent completion: final answer lost (truncated-by-retention) + duplicate settle events + deliveryStatus stuck pending | CORE · U/NA/E3 | [阅读归档](../L2_harness_tasks/OC-I155119/issue_and_comments.md) |
| OC-I164545 | T-L2-03 | Local plugins cannot opt in to api.runtime.gateway: dispatchTrustedPluginGatewayMethod accepts only bundled or trustedOfficialInstall, so no local plugin can steer or abort a running session | CORE · T/M/E1 | [阅读归档](../L2_harness_tasks/OC-I164545/issue_and_comments.md) |
| OC-I84647 | T-L2-02 | gateway: scheduler fairness — human-facing sessions starved by concurrent background agents on shared event loop | CORE · U/NA/E3 | [阅读归档](../L2_harness_tasks/OC-I84647/issue_and_comments.md) |
| OC-I116227 | T-L2-02 | [Bug]: subagent registry persistence scales superlinearly with concurrent runs | CORE · D/NA/E1 | [阅读归档](../L2_harness_tasks/OC-I116227/issue_and_comments.md) |
| OC-I169777 | T-L2-03 | [Bug]: Gateway shutdown hangs ~325 s in restart.close.transcript-workers with no active work (reconcile pool join is unbounded) | CORE · D/NA/E1 | [阅读归档](../L2_harness_tasks/OC-I169777/issue_and_comments.md) |
| OC-P82765 | T-L2-02 | fix(agents): prioritize manual session turns | CORE | [阅读归档](../L2_harness_tasks/OC-P82765/pull_request.md) |
| OC-P122764 | T-L2-02 | fix(queue): arbitrate shared capacity across grouped lanes | CORE | [阅读归档](../L2_harness_tasks/OC-P122764/pull_request.md) |
| OC-P167683 | T-L2-02、T-L3-04 | fix(queue): prevent background starvation under sustained traffic | CORE | [阅读归档](../L2_harness_tasks/OC-P167683/pull_request.md) |
| OC-P116286 | T-L2-02 | fix(agents): prevent registry stalls during large fan-outs | CORE | [阅读归档](../L2_harness_tasks/OC-P116286/pull_request.md) |
| OC-P170493 | T-L2-03 | fix: prevent stalled transcript workers from blocking Gateway shutdown | CORE | [阅读归档](../L2_harness_tasks/OC-P170493/pull_request.md) |
| OC-P171570 | T-L2-02、T-L2-03 | fix: keep pending Code Mode calls active during recovery | CORE | [阅读归档](../L2_harness_tasks/OC-P171570/pull_request.md) |

---

## 3. L3：个人模型请求与额度

每个来源在主层只保存一份，关联主题包括案例支持、现有控制和反证。

| 来源ID | 主题关联 | 标题 | 范围／案例编码 | 证据文件 |
| :--- | :--- | :--- | :--- | :--- |
| A01 | T-L3-03 | Rate limits ／ OpenAI API | CORE | [阅读归档](../L3_client_requests/A01/A01.txt) |
| A02 | T-L3-03 | Rate limits - Claude Platform Docs | CORE | [阅读归档](../L3_client_requests/A02/A02.txt) |
| A06 | T-L3-04、T-L4-01、T-L4-04 | HiveMind: OS-Inspired Scheduling forConcurrent LLM Agent Workloads | CORE | [阅读归档](../L3_client_requests/A06/A06.txt) |
| A07 | 资料与版本背景 | [2604.17111] HiveMind: OS-Inspired Scheduling for Concurrent LLM Agent Workloads | CORE | [阅读归档](../L3_client_requests/A07/A07.txt) |
| A17 | T-L3-01、T-L4-03 | > ## Documentation Index | CORE | [阅读归档](../L3_client_requests/A17/A17.txt) |
| A21 | T-L3-02 | feat(proxy): opt-in per-request budget reservation cost cap | CORE · T/M/E3 | [阅读归档](../L3_client_requests/A21/A21.txt) |
| A22 | 资料与版本背景 | fix(langgraph): do not retry requests 4xx in default_retry_on | CORE · P/M/E3 | [阅读归档](../L3_client_requests/A22/A22.txt) |
| A23 | 资料与版本背景 | default_retry_on retries requests.HTTPError 4xx responses instead of only 5xx | CORE · P/M/E3 | [阅读归档](../L3_client_requests/A23/A23.txt) |
| A27 | T-L3-03 | Models - Docs by LangChain | CORE | [阅读归档](../L3_client_requests/A27/A27.txt) |
| A29 | T-L3-02 | GitHub issue/PR discussion | CORE · T/M/E3 | [阅读归档](../L3_client_requests/A29/A29.txt) |
| L08 | 资料与版本背景 | HiveMind: OS-Inspired Scheduling forConcurrent LLM Agent Workloads | CORE | [阅读归档](../L3_client_requests/L08/L08.txt) |
| CX43337 | 资料与版本背景 | Account-specific capacity errors across Codex models despite a fully available weekly allowance | ADJACENT · U/NA/E3 | [阅读归档](../L3_client_requests/CX43337/43337.txt) |
| CX48296 | 资料与版本背景 | Pro — multiple weekly resets exhausted during LexPulse / ~50% Subagent usage / Analytics periods marked incomplete | ADJACENT · U/NA/E3 | [阅读归档](../L3_client_requests/CX48296/48296.txt) |
| CC01 | 资料与版本背景 | [FEATURE] Budget-aware subagent orchestration: premium-model fan-out burned ~70% of a weekly Fable 5.1 allowance, mostly on subagents killed by the rate limit · Issue #94770 · anthropics/claude-code · GitHub | ADJACENT · U/NA/E3 | [阅读归档](../L3_client_requests/CC01/issue_94770.txt) |
| CC05 | 资料与版本背景 | [BUG] Headless `claude -p` (entrypoint `sdk-cli`) uses ~1.8× more of the 5-hour window per token than interactive `cli`, same workload · Issue #97074 · anthropics/claude-code · GitHub | ADJACENT · U/NA/E3 | [阅读归档](../L3_client_requests/CC05/issue_97074.txt) |
| CCD03 | T-L3-02 | costs | ADJACENT | [阅读归档](../L3_client_requests/CCD03/D03.txt) |
| CCD07 | T-L3-01 | errors | CORE | [阅读归档](../L3_client_requests/CCD07/D07.txt) |
| CCD09 | 资料与版本背景 | statusline | ADJACENT | [阅读归档](../L3_client_requests/CCD09/D09.txt) |
| D892 | T-L3-01 | [Bug Report] 429 限流不自动恢复：`dsh-llm-retry` 在网关 `Retry-After` 超过默认 `maxDelayMs`（10s）时直接放弃重试 · deepseek-ai/deepseek-harness · Discussion #892 · GitHub | CORE · P/M/E2 | [阅读归档](../L3_client_requests/D892/D892.txt) |
| D3128 | T-L3-01 | [Bug] dsh-llm-pi-ai ignores Retry-After when retrying — TPM-limited retries always fail · deepseek-ai/deepseek-harness · Discussion #3128 · GitHub | CORE · P/M/E2 | [阅读归档](../L3_client_requests/D3128/D3128.txt) |
| D3852 | T-L3-01 | RFC: 429 恢复是一条跨五层的缺陷链——#3128 必须晚于 #892 落地 · deepseek-ai/deepseek-harness · Discussion #3852 · GitHub | CORE · P/NA/E3 | [阅读归档](../L3_client_requests/D3852/D3852.txt) |
| D7926 | T-L3-01 | dsh-llm-pi-ai drops Retry-After, so 503/429 retries fall back to a 500 ms local backoff · deepseek-ai/deepseek-harness · Discussion #7926 · GitHub | CORE · P/M/E3 | [阅读归档](../L3_client_requests/D7926/D7926.txt) |
| D5712 | 资料与版本背景 | 同时不同模型执行不同任务会直接触发429错误 · deepseek-ai/deepseek-harness · Discussion #5712 · GitHub | ADJACENT · U/NA/E3 | [阅读归档](../L3_client_requests/D5712/D5712.txt) |
| D6659 | 资料与版本背景 | [Bug] 低并发场景下频繁出现服务器繁忙与turn error，零点时段规律性服务中断 · deepseek-ai/deepseek-harness · Discussion #6659 · GitHub | ADJACENT · U/NA/E3 | [阅读归档](../L3_client_requests/D6659/D6659.txt) |
| D6959 | 资料与版本背景 | [Bug] llm-deepseek: SSE keep-alives re-arm the idle watchdog forever — a 300s idle timeout actually waited 17.7 min, and a chatty provider can hang a turn unboundedly · deepseek-ai/deepseek-harness · Discussion #6959 · GitHub | CORE · P/M/E3 | [阅读归档](../L3_client_requests/D6959/D6959.txt) |
| DS_retry | T-L3-01 | packages/llm/llm-retry/src/index.ts | CORE | [阅读归档](../L3_client_requests/DS_retry/source_retry.txt) |
| DS_pi_stream | T-L3-01 | packages/llm/llm-pi-ai/src/stream.ts | CORE | [阅读归档](../L3_client_requests/DS_pi_stream/source_pi_stream.txt) |
| DS_llm_adapter | 资料与版本背景 | packages/llm/llm-deepseek/src/protocols/chat-completions/adapter.ts | ACCESS_FAILED | [阅读归档](../L3_client_requests/DS_llm_adapter/source_llm_adapter.txt) |
| R06 | T-L4-02、T-L4-04 | LLM adapter/stream plugin interception, cancellation | CORE | [阅读归档](../L3_client_requests/R06/R06.txt) |
| C04 | T-L3-01 | AWS Builder Center | METHOD | [阅读归档](../L3_client_requests/C04/C04_browser.txt) |
| OC-D15 | T-L3-03、T-L4-02 | OpenClaw：docs/concepts/model-providers/custom-providers.md | CORE | [阅读归档](../L3_client_requests/OC-D15/custom-providers.md) |
| OC-D16 | T-L3-01、T-L3-03 | OpenClaw：docs/concepts/model-failover.md | CORE | [阅读归档](../L3_client_requests/OC-D16/model-failover.md) |
| OC-D17 | T-L3-01 | OpenClaw：docs/concepts/retry.md | CORE | [阅读归档](../L3_client_requests/OC-D17/retry.md) |
| OC-D21 | T-L3-01、T-L3-03、T-L4-02 | OpenClaw：docs/plugins/sdk-provider-plugins/runtime-hooks.md | CORE | [阅读归档](../L3_client_requests/OC-D21/runtime-hooks.md) |
| OC-S13 | T-L3-01、T-L3-03 | OpenClaw：src/plugins/runtime/runtime-llm.runtime.ts | CORE | [阅读归档](../L3_client_requests/OC-S13/runtime-llm.runtime.ts) |
| OC-S14 | T-L3-01、T-L3-03 | OpenClaw：src/agents/simple-completion-execution.ts | CORE | [阅读归档](../L3_client_requests/OC-S14/simple-completion-execution.ts) |
| OC-I152360 | T-L3-01、T-L3-03 | Plugin runtime.llm.complete never rotates auth profiles: a rate-limited first profile fails every plugin completion while agent turns fail over | CORE · P/M/E2 | [阅读归档](../L3_client_requests/OC-I152360/issue_and_comments.md) |
| OC-P152489 | T-L3-01 | fix: plugin LLM completions stay on a rate-limited first auth profile | CORE | [阅读归档](../L3_client_requests/OC-P152489/pull_request.md) |

---

## 4. L4：平台多用户请求与协同

每个来源在主层只保存一份，关联主题包括案例支持、现有控制和反证。

| 来源ID | 主题关联 | 标题 | 范围／案例编码 | 证据文件 |
| :--- | :--- | :--- | :--- | :--- |
| A04 | T-L3-03、T-L4-01、T-L4-03 | Router - Load Balancing ／ liteLLM | CORE | [阅读归档](../L4_platform_requests/A04/A04.txt) |
| A05 | T-L3-02 | Budgets, Rate Limits ／ liteLLM | CORE | [阅读归档](../L4_platform_requests/A05/A05.txt) |
| A13 | T-L3-03、T-L4-01、T-L4-03、T-L4-04 | Dynamic TPM/RPM Allocation ／ liteLLM | CORE | [阅读归档](../L4_platform_requests/A13/A13.txt) |
| A14 | T-L3-02、T-L3-03 | [Beta] Separate ITPM / OTPM Rate Limits ／ liteLLM | CORE | [阅读归档](../L4_platform_requests/A14/A14.txt) |
| A15 | 资料与版本背景 | Incident Report: Retry Breadcrumb Memory Growth Causing OOM on v1.100.0 ／ liteLLM | CORE · U/NA/E1 | [阅读归档](../L4_platform_requests/A15/A15.txt) |
| A16 | 资料与版本背景 | Integrations - Portkey Docs | CORE | [阅读归档](../L4_platform_requests/A16/A16.txt) |
| A18 | 资料与版本背景 | Integrations - Portkey Docs | CORE | [阅读归档](../L4_platform_requests/A18/A18.txt) |
| A19 | T-L3-03 | Usage-based Rate Limiting ／ Agent Router | CORE | [阅读归档](../L4_platform_requests/A19/A19.txt) |
| A24 | T-L3-04、T-L4-01、T-L4-02、T-L4-04 | Task Queue Priority and Fairness ／ Temporal Documentation | CORE | [阅读归档](../L4_platform_requests/A24/A24.txt) |
| A25 | T-L3-03、T-L4-03 | Rate-limit downstream APIs with separate Task Queues ／ Temporal Documentation | CORE | [阅读归档](../L4_platform_requests/A25/A25.txt) |
| A26 | T-L3-04、T-L4-01、T-L4-03 | [BETA] Request Prioritization ／ liteLLM | CORE | [阅读归档](../L4_platform_requests/A26/A26.txt) |
| A30 | 资料与版本背景 | Envoy AI Gateway is becoming Agent Router, an Agentic AI Foundation project ／ Agent Router | CORE | [阅读归档](../L4_platform_requests/A30/A30.txt) |
| R01 | T-L4-02 | Conversation state ／ OpenAI API | CORE | [阅读归档](../L4_platform_requests/R01/R01.txt) |
| C08 | T-L4-03 | Pattern: Using ray.wait to limit the number of pending tasks — Ray 2.58.0 | METHOD | [阅读归档](../L4_platform_requests/C08/C08.txt) |
| C10 | T-L4-01、T-L4-03 | API Priority and Fairness ／ Kubernetes | METHOD | [阅读归档](../L4_platform_requests/C10/C10.txt) |

---

## 5. 范围外、排除与失败访问记录

这些来源保存检索与方法背景，退出当前四层痛点的剩余排序。

| 来源ID | 标题 | 原因 | 证据文件 |
| :--- | :--- | :--- | :--- |
| A03 | Page Not Found ／ liteLLM | ACCESS_FAILED | [阅读归档](../_archive/scope_materials/access_failed/A03/A03.txt) |
| A08 | A08 | ACCESS_FAILED | [阅读归档](../_archive/scope_materials/access_failed/A08/A08.txt) |
| A09 | Page Not Found ／ Agent Router | ACCESS_FAILED | [阅读归档](../_archive/scope_materials/access_failed/A09/A09.txt) |
| A12 | Dynamic Request Batching — Ray 2.58.0 | BACKGROUND | [阅读归档](../_archive/scope_materials/background/A12/A12.txt) |
| L01 | Agentix: An Efficient Serving Engine for LLM Agents as General Programs ／ USENIX | BACKGROUND | [阅读归档](../_archive/scope_materials/background/L01/L01.txt) |
| L02 | JITServe: SLO-aware LLM Serving with Imprecise Request Information ／ USENIX | BACKGROUND | [阅读归档](../_archive/scope_materials/background/L02/L02.txt) |
| L03 | Parrot: Efficient Serving of LLM-based Applications with Semantic Variable ／ USENIX | BACKGROUND | [阅读归档](../_archive/scope_materials/background/L03/L03.txt) |
| L04 | Fairness in Serving Large Language Models ／ USENIX | BACKGROUND | [阅读归档](../_archive/scope_materials/background/L04/L04.txt) |
| L05 | Orla: A Library for Serving LLM-Based Multi-Agent Systems | BACKGROUND | [阅读归档](../_archive/scope_materials/background/L05/L05.txt) |
| L06 | Decoupling Readiness from Release for Tail-Aware Scheduling of Agentic LLM Workflows | BACKGROUND | [阅读归档](../_archive/scope_materials/background/L06/L06.txt) |
| L09 | AgentOpt v0.1 Technical Report:Client-Side Optimization for LLM-Based Agent | BACKGROUND | [阅读归档](../_archive/scope_materials/background/L09/L09.txt) |
| L10 | AgentServe: Algorithm-System Co-Design for Efficient Agentic AI Serving on a Consumer-Grade GPU | BACKGROUND | [阅读归档](../_archive/scope_materials/background/L10/L10.txt) |
| L11 | AgentReplay: Token-Wise Trace ReplayIs Essential for Fair Serving SystemPerformance Benchmarking | BACKGROUND | [阅读归档](../_archive/scope_materials/background/L11/L11.txt) |
| L12 | PackServe: SLO-Aware Request Scheduling for Agentic LLM Serving at Scale | BACKGROUND | [阅读归档](../_archive/scope_materials/background/L12/L12.txt) |
| L13 | SARA: SLO-Aware Resource Allocation for Disaggregated Agentic LLM Services | BACKGROUND | [阅读归档](../_archive/scope_materials/background/L13/L13.txt) |
| L14 | SwarmX: Agentic Scheduling for Low-Latency Agentic Systems | BACKGROUND | [阅读归档](../_archive/scope_materials/background/L14/L14.txt) |
| L15 | AgentPerfBench: A Benchmarking and Evaluation Suite for Inference Performance of Agentic LLMs | BACKGROUND | [阅读归档](../_archive/scope_materials/background/L15/L15.txt) |
| L16 | Murakkab: Resource-Efficient Agentic Workflow Orchestration in Cloud Platforms | BACKGROUND | [阅读归档](../_archive/scope_materials/background/L16/L16.txt) |
| L16_official | Murakkab: Resource-Efficient Agentic Workflow Orchestration in Cloud Platforms ／ USENIX | BACKGROUND | [阅读归档](../_archive/scope_materials/background/L16_official/L16_official.txt) |
| CC15 | [Retracted] Not a Claude Code bug: session drops were caused by a test suite deleting credentials via an inherited CLAUDE_CONFIG_DIR · Issue #93127 · anthropics/claude-code · GitHub | EXCLUDED | [阅读归档](../_archive/scope_materials/excluded/CC15/issue_93127.txt) |
| D8001 | [Bug][Windows] 0.1.7-rc.2: 窗口关闭死锁 / ACL沙箱权限崩溃(Win32 5) / 子代理卡片失踪与硬锁8并发 / SSRF误杀TUN代理 · deepseek-ai/deepseek-harness · Discussion #8001 · GitHub | EXCLUDED | [阅读归档](../_archive/scope_materials/excluded/D8001/D8001.txt) |
| Q01 | Optimization and Tuning - vLLM | BACKGROUND | [阅读归档](../_archive/scope_materials/background/Q01/Q01.txt) |
| Q02 | Engine Arguments - vLLM | BACKGROUND | [阅读归档](../_archive/scope_materials/background/Q02/Q02.txt) |
| Q03 | priority_scheduling.html | ACCESS_FAILED | [阅读归档](../_archive/scope_materials/background/Q03/Q03.txt) |
| Q04 | Fast mode ／ OpenAI API | BACKGROUND | [阅读归档](../_archive/scope_materials/background/Q04/Q04.txt) |
| Q05 | Service tiers - Claude Platform Docs | BACKGROUND | [阅读归档](../_archive/scope_materials/background/Q05/Q05.txt) |
| Q06 | Fast Distributed Inference Serving for Large Language Models | BACKGROUND | [阅读归档](../_archive/scope_materials/background/Q06/Q06.txt) |
| Q07 | Fairness in Serving Large Language Models ／ USENIX | BACKGROUND | [阅读归档](../_archive/scope_materials/background/Q07/Q07.txt) |
| Q08 | Server Arguments - SGLang Documentation | BACKGROUND | [阅读归档](../_archive/scope_materials/background/Q08/Q08.txt) |
| C13 | Page not found — Ray 2.58.0 | ACCESS_FAILED | [阅读归档](../_archive/scope_materials/access_failed/C13/C13.txt) |
