# TaskLoom 共享访问与同步互斥 接入与验收记录

- 工作包：IKR9K3（共享访问与同步互斥）
- 分支：`feat/access-coordination`（本分支基于 9b95850；已合并 `origin/main` = 644aae0d2cd24489c69ac38e36bd74b71af3ae67，见 §5.1）
- 记录日期：2026-10-27（初次接入）；2026-10-29（IKR9K3 审查修复，见 §5.1）；2026-10-30（合并 origin/main，协调者模块改名 `coordination.py`）；2026-10-30（PR #15 审查修复，见 §5.2）
- 验证环境：Linux 容器（`docker.m.daocloud.io/library/python:3.12-slim`，Python 3.12.15，x86_64/arm64 均可），`uv sync --locked --group dev`

## 1. 本次解决的实际问题

个人端同时跑多个根任务时，每个任务都会各自启动一份工具进程去解析自己的暂存文档。同一批资料被两个任务引用时，
解析动作被重复执行两次，资源（工具位、内存、CPU 配额）被重复占用一次，账本上也看不出这次工作到底由谁承担。

本次接入一套**跨任务的共享访问协调**，落在执行器与工具子进程之间：

1. 同一份「资料解析」准备工作，在途时只真实执行一次，其余使用者等待并复用同一份成果。
2. 工具子进程执行前必须持有协调者签发的访问许可，许可绑定资源键、输入版本、持有者与有效期。
3. 文件操作由资源锁保护：多读者并行，写独占；多资源按固定顺序取锁，反向请求不会互锁。
4. 共享与等待全部记账，可在一条工具链记录里读出「谁执行的、共享了几次、等了多久、成果版本是什么」。

## 2. 模块与入口

| 位置 | 职责 |
| --- | --- |
| `src/taskloom/policies/access.py` | 并发原语：SingleFlight（合并同键在途工作，容量耗尽抛 `CapacityExhausted`）、ReadWriteLock（多读者并行 / 写独占 / 写者优先）。协调者调用前两者；KeyedMutex 仍保留但协调者不再使用（多资源按排序逐个取读写锁即可避免互锁）。 |
| `src/taskloom/execution/coordination.py` | 协调者：规范化资源键、许可签发与校验、跨进程文件租约、共享成果存储、账本。（原 `execution/access.py`；因 main PR #11 在同一新建了同名但完全不同的路径助手模块 `execution/access.py`（`local_path`/`readable`），合并时本模块改名为 `coordination.py`，公共接口签名无变化。） |
| `src/taskloom/adapters/executor.py` | 执行器接线：判断 owner / waiter / 复用，签发许可，回收共享成果，写入证据。协调者由 `ProcessExecutor.__init__` 在未注入时按 `profile/access` 默认构造；`bootstrap/apps.py` 的 `executor_app` 只构造 `ProcessExecutor`，没有单独的协调者装配点。 |
| `src/taskloom/execution/tool.py` | 工具子进程接线：校验许可、检查文件身份与权限、持锁执行、发布前复核输入版本。 |
| `src/taskloom/client/service.py` | 本 commit 未修改该文件：共享证据随 result 透传（`service.py:971` `attempt.result = state.result`）进入工具链记录。 |

## 3. 关键逻辑

### 3.1 规范化资源键

```python
ResourceKey.of(request, workspace_root=...)
# tenant_id + workspace_root + input_version + tool_name + arguments_digest
# + work_iterations + approved_external_path + command_approved + approved_command_cwd
```

- `input_version` 是暂存文档的内容摘要，相同资料得到相同键；
- `tenant_id`、执行器 `workspace_root` 与全部审批字段参与键，任何一项不同都不是同一资源；
- `arguments_digest` 让不同参数（如 grep 的 pattern）落到不同资源；
- `work_iterations` 参与键，因为重活轮数不同意味着成果不同。

**只有纯函数工具共享**（`coordination.SHAREABLE_TOOLS` 白名单：`file_stats`、`grep`、`list_files`、`top_words`），
判断标准是结果只由暂存输入 + 参数决定、无副作用、不读外部路径、不需要审批。`read_file`（读工作区或外部文件，内容可随时改变，且受审批约束）
与 `run_command`（有副作用）以及任何带审批字段的请求一律不共享：不查共享缓存、不申领、不发布、不持共享锁，每次都真实执行。

### 3.2 owner / waiter / 复用 三种角色

执行器收到 `POST /execute` 后：

1. 先做短临界区登记（写入状态；同键已有 owner 在途时不占工具位，因为它多半会成为 waiter），随即释放全局账本锁——文件检查与进程启动都不在锁内。
2. 校验暂存工作区（输入版本、租户、文件身份）。
3. 查共享存储：命中且未过期 → 直接复用成果，角色 `shared`，不启动任何进程。
4. 未命中则向协调者申领资源键：
   - 申领成功 → 角色 `owner`，真实启动工具进程；
   - 申领失败 → 角色 `waiter`，不启动进程、不占工具位，**先持久化（`share_role="waiter"`）再返回**，等待 owner 的成果；
     同时等待者超过 `max_waiters`（默认 64）时退化为不共享，直接在已占的工具位上执行。

等待者拿到成果后按自己的 attempt 重新写产物，`artifact_id` 与 `artifact_sha256` 各自独立。

### 3.3 取消与失败

- 取消意图先落盘；执行器在 `Popen` 前、后各复查一次，`Popen` 后发现已取消立即停止进程组。
  `cancel` 遇到仍在登记/启动中的 attempt 会等待其启动决定完成，绝不在进程可能稍后启动时宣告已停止；
- 等待者可以单独取消：它只是退出等待，owner 继续跑，成果照常发布；
- 拥有者负责共享工作的结束与回收：成果发布后等待者被唤醒，等待者各自拿一份；
- owner 失败或被取消时，等待者重新申领（最多 3 轮）：申领成功则自己成为 owner 真实执行（需有空闲工具位，否则 `EXECUTOR_FULL` 可重试失败），
  否则继续等待新的 owner；SingleFlight 容量耗尽时等待者退化为不共享直接执行；
- 执行器重启时，从未启动进程的 waiter 判 `FAILED` + quiescent（错误码前缀 `EXECUTOR_RESTARTED`），客户端按普通失败处理，不进入 `TOOL_UNKNOWN` 隔离。
- 结算时序：`_watch` 先把 outcome / error（含 stderr 诊断）/ result（含共享发布）全部算进局部变量，再无 await 地依次写 error、result、outcome，最后写 quiescent。

### 3.4 访问许可

协调者为每次工具执行签发一张许可（HMAC-SHA256 签名，密钥保存在 profile 目录，权限 0600）。
启动时校验密钥：须为本用户的普通文件、无组/其他权限位、至少 32 字节，否则重建（无法重建时报 `ACCESS_SECRET_INVALID`）。

**许可只是防误用的完整性校验，不是安全边界**：工具进程（含 `run_command` 启动的命令）与执行器同一 uid，能读取密钥自行签发；
许可也不记录已用，有效期内可重放。它防的是脱离执行器直接启动工具、或拿错 attempt 的 spec，而不是恶意进程。

| 字段 | 含义 |
| --- | --- |
| `resource` | 规范化资源键 |
| `holder` / `owner` | 持有者 attempt 与拥有者 attempt |
| `mode` | `read` / `write` |
| `input_version` | 绑定的输入版本 |
| `issued_at` / `expires_at` | 有效期 |
| `signature` | 签名 |

工具子进程在 `main()` 一进入就校验许可：签名不符、已过期、持有者不符、输入版本不符，四种情况各自拒绝执行。

### 3.5 文件访问

- 工具子进程对暂存输入做身份与权限检查：必须是不链接的普通文件、属于当前用户、可读；
- 共享准备的 owner 执行期间持有该共享成果的文件租约（`fcntl.flock`，写模式 `LOCK_EX`）；不共享的工具不持锁。
  原先的 `workspace:<root>` 读锁没有任何写者（客户端写暂存输入不加锁），形同虚设，已删除；暂存输入的一致性改由执行前后两次核对输入版本保证；
- 多资源按路径排序后依次取锁，因此反向请求不会互锁；
- 发布产物前再次核对输入版本，防止执行期间暂存内容被改动。

### 3.6 记账

协调者在 `access/ledger.jsonl` 追加一行行记录：`permit_issued`、`lock_wait`（含等待毫秒与排序后的键）、
`shared_published`（拥有者与共享次数）、`shared_failed`、`secret_replaced`。

上限：账本超过 1 MiB 轮转为 `ledger.jsonl.1`（只保留一份，总量 < 2 MiB）；已发布成果最多 256 份，过期（`share_ttl`）或超量最旧者连同文件删除，
启动时清扫过期/损坏的共享文件；失败记录超过 `share_ttl` 遗忘；使用者集合只为在途/等待/已发布的键保留；读写锁对象无人持有即回收。

成本归属：共享准备的执行成本只记在 owner 的 attempt（及其根任务）上，waiter 记 0；同账户不同根任务之间不做分摊，这是有意的简化。

## 4. 共享证据如何出现在工具链记录里

执行器在工具结果里追加 `result["access"]`：

```json
{
  "resource": "<规范化资源键>",
  "role": "owner | waiter | shared",
  "owner": "<真正执行准备的 attempt>",
  "version": "<成果版本>",
  "users": 2,
  "waited_ms": 12.4,
  "permit_id": "<许可编号>"
}
```

客户端把这块证据原样带到 attempt 记录，因此 `GET /api/v1/tasks/{id}/graph` 里能直接读到：
共享次数（`users`）、等待时长（`waited_ms`）、成果版本（`version`）、以及这次到底是自己在跑还是在复用。

## 5. 验证记录

命令（在容器内）：

```bash
uv sync --locked --group dev
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy
.venv/bin/python -m pytest -q tests/unit tests/integration tests/l4
.venv/bin/python -m pytest -q tests/smoke
.venv/bin/lint-imports
```

结果（本次 head）：

| 项目 | 结果 |
| --- | --- |
| ruff format --check | 206 files already formatted |
| ruff check | All checks passed |
| mypy | Success: no issues found in 53 source files |
| pytest unit + integration + l4 | 341 passed, 1 skipped in 105.92s |
| pytest smoke | 6 passed in 35.27s |
| lint-imports | Contracts: 3 kept, 0 broken |

新增测试：

- `tests/unit/test_execution_access.py`（14 例）：资源键稳定性与区分性、许可往返与四种拒绝、签名按密钥隔离、
  同键只初始化一次、取消一个等待者另一个仍拿到结果、失败后可重新申请、发布过期、读并行 + 写独占、
  反向请求不死锁、文件租约读共享 / 写等待 / 固定顺序、账本记录归属。
- `tests/integration/test_shared_access.py`（4 例）：两个真实任务只产生一次实际初始化、
  后续任务复用已发布成果、取消等待者不影响拥有者、工作区被改动时不得用共享成果顶替。

### 5.1 IKR9K3 审查修复验证记录（2026-10-29）

审查发现 3 个并发缺陷，已在本分支单一修复 commit 中修复
（parent = f570222564a33d4d87224347a466905cdf2df353；sha 见推送/PR 记录，文档与代码同 commit）。

| 缺陷 | 修复 | 回归测试 |
| --- | --- | --- |
| P1 `await_outcome` 丢失唤醒：`publish`/`fail` 解析旧 future 后又补一个全新的未解析 future，且 `wait()` 挂起前不查已发布结果，错过调度间隙的 waiter 永久挂起（`service.py:872` 轮询随之死循环） | `execution/coordination.py`（当时为 `execution/access.py`，合并后改名）：`wait()` 先查 `lookup` 与新增的失败记录 `_failed`，均未命中才注册 future，检查与注册之间无 await 间隙；`publish`/`fail` 只 pop 并解析已有 future，不再补空 future；`claim` 清除历史失败记录 | `tests/unit/test_execution_access.py::test_await_outcome_after_publish_is_served_immediately`、`::test_await_outcome_after_fail_raises_immediately` |
| P1 `_watch` 成功路径先置 `quiescent` 后置 `outcome`：两次赋值之间有 `await access.publish`（写锁+原子写）的真实窗口，`service.py:872` 轮询观察到 quiescent=True + PENDING 即误报 TOOL_PENDING | `adapters/executor.py` `_watch()`：`state.quiescent` 移到全部终态字段落定之后、同一同步块内置位；except 分支改用局部变量 `quiescent` 判定 FAILED/UNKNOWN，不读半更新状态 | `tests/integration/test_shared_access.py::test_settling_never_shows_quiescent_pending_to_poller`（SlowPublish 子类把 publish 加宽 0.3s，按 service 方式轮询，断言从不观察到脏中间态，最终 SUCCEEDED 且 role=owner） |
| P2 claim 成功后 `_launch` 抛异常（`cgroups.create`/`issue_permit`/spec 落盘）不通知协调者，键滞留 in-flight，此后同键 run() 全部沦为 waiter 永久等待 | `adapters/executor.py` `run()`：新增 `claimed` 标志，异常路径对已 claim 的键调用 `access.fail(key, "类型名: 消息")` 再抛；`_launch` 的 Popen OSError 分支自行上报且不抛异常，`fail()` 幂等，不会重复失败 | `tests/integration/test_shared_access.py::test_launch_failure_frees_the_claim_for_the_next_attempt`（断言恰好一条 shared_failed 账本记录，随后同键 run() 成为 owner 并成功） |

缺陷实证（临时脚本，不入库）：

- prove1.py（macOS 宿主，Python 3.12.13）：claim→publish→await_outcome。
  修复前输出 `BUG STILL PRESENT: await_outcome hangs after publish`；
  修复后输出 `SERVED: 39f94024cbcf740958d8bb6e25095e33ee260776744698ff4e0d43a4454bf72f`。
- prove2.py（Linux 容器，SlowPublish 加宽 publish 窗口 0.3s，复刻 `service.py:872` 轮询）：
  修复前（对 f570222 原始树）输出 `DIRTY MIDSTATE (bug) PENDING`；
  修复后输出 `CLEAN SUCCEEDED`。

已合并 origin/main 644aae0（PR #9–#12）并重跑全量验证；协调者模块因与 main PR #11 的
`execution/access.py`（Windows/WSL 路径助手）同名撞车而改名为 `execution/coordination.py`，
公共接口签名无变化，`tests/unit/test_execution_access.py` 与 `tests/integration/test_shared_access.py`
文件名保持不变。三个冲突文件（`execution/access.py`、`execution/tool.py`、`adapters/executor.py`）
均按「两侧功能都保留」融合。

验证（head = 本 merge commit（f319ddf × origin/main 644aae0）的工作树，sha 见推送/PR 记录；
环境与上文相同的 Linux 容器 Python 3.12.15，`uv sync --locked --group dev`，命令与 §5 相同）：

| 项目 | 结果 |
| --- | --- |
| pytest tests/unit/test_execution_access.py tests/integration/test_shared_access.py（定向） | 22 passed in 5.67s |
| pytest tests/unit tests/integration tests/l4 | 376 passed, 1 skipped in 177.56s（修复 commit 基线 345 + main 侧 PR #11/#12 新增测试，0 failed，无回退） |
| pytest tests/smoke | 7 passed in 67.00s（基线 6 + main 侧新增 test_chat_workbench） |
| ruff format --check | 221 files already formatted |
| ruff check | All checks passed! |
| mypy src | Success: no issues found in 58 source files |
| lint-imports | Contracts: 3 kept, 0 broken. |
| pytest tests/linux | 未执行（非特权容器无 cgroup v2） |

### 5.2 PR #15 审查修复（2026-10-30）

| 问题 | 修复 | 回归测试（`tests/integration/test_shared_access.py` 除注明外） |
| --- | --- | --- |
| 共享键缺字段、所有工具一律共享 | `SHAREABLE_TOOLS` 白名单 + `ResourceKey` 补 tenant/workspace_root/审批字段；`read_file`/`run_command` 不查缓存不发布 | `test_an_unapproved_external_read_is_never_served_from_cache`、`test_a_rewritten_workspace_file_is_read_again`、`test_the_same_approved_append_runs_every_time`；单测 `test_resource_key_covers_account_workspace_and_approvals`、`test_only_pure_staged_input_tools_share` |
| 取消与启动竞态 | `Popen` 前后复查取消意图；`cancel` 等待 `starting` 中的 attempt | `test_cancel_during_process_creation_stops_the_late_process`、`test_cancel_before_admission_starts_nothing` |
| waiter 不持久化 | 等待前 `_save`（`share_role="waiter"`），重启判 FAILED+quiescent | `test_a_waiter_survives_an_executor_restart_as_a_clean_failure` |
| `_watch` 结算时序 | 局部变量算好后无 await 写入 | `test_a_failed_read_never_shows_a_settled_outcome_before_quiescent`；`tests/integration/test_chat_tools.py::test_missing_file_is_observation_and_agent_can_respond` |
| 无上限表/账本、waiter 不重新竞争、容量耗尽失败、密钥校验、读锁虚设、无写者优先、waiter 占槽 | 见 §3 | `test_a_waiter_takes_over_when_the_owner_is_cancelled`、`test_an_exhausted_single_flight_runs_the_waiter_unshared`；`tests/unit/test_execution_access.py` 上限/密钥/锁回收用例；`tests/unit/test_access.py` 写者优先用例 |

## 6. 已知边界

- 共享判定以内容摘要为准：两份内容完全相同但文件名不同的资料会被视为不同资源，不共享。
- 共享成果只在协调者的保留期内有效（默认 600 秒），过期后重新执行。
- 跨进程互斥依赖 POSIX 文件锁，仅在 Linux 目标平台可用；macOS 上工具执行本身不可运行（`/proc` 依赖）。
- 许可校验发生在工具子进程入口，不替代执行器侧的输入版本与租户校验，两者同时生效。
- 等待者不启动进程、不占工具槽（`ProcessExecutor._running` 不计 waiter），数量由 `max_waiters` 限制。
- 共享只覆盖单个执行器进程内的在途合并；跨执行器重启只复用已落盘且未过期的成果。
