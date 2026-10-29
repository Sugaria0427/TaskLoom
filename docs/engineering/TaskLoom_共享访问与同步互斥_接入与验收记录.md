# TaskLoom 共享访问与同步互斥 接入与验收记录

- 工作包：IKR9K3（共享访问与同步互斥）
- 分支：`feat/access-coordination`（本分支基于 9b95850；当前真实 `origin/main` = 644aae0d2cd24489c69ac38e36bd74b71af3ae67，合并前需确认与最新 main 的可合性）
- 记录日期：2026-10-27（初次接入）；2026-10-29（IKR9K3 审查修复，见 §5.1）
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
| `src/taskloom/policies/access.py` | 并发原语：SingleFlight（合并同键在途工作）、ReadWriteLock（多读者并行 / 写独占）、KeyedMutex（多资源固定顺序）。本次开始被生产代码真实调用。 |
| `src/taskloom/execution/access.py` | 协调者：规范化资源键、许可签发与校验、跨进程文件租约、共享成果存储、账本。 |
| `src/taskloom/adapters/executor.py` | 执行器接线：判断 owner / waiter / 复用，签发许可，回收共享成果，写入证据。 |
| `src/taskloom/execution/tool.py` | 工具子进程接线：校验许可、检查文件身份与权限、持锁执行、发布前复核输入版本。 |
| `src/taskloom/client/service.py` | 本 commit 未修改该文件：共享证据随 result 透传（`service.py:971` `attempt.result = state.result`）进入工具链记录。 |

## 3. 关键逻辑

### 3.1 规范化资源键

```python
ResourceKey.of(request)  # input_version + tool_name + arguments_digest + work_iterations
```

- `input_version` 是暂存文档的内容摘要，相同资料得到相同键；
- `arguments_digest` 让不同参数（如 grep 的 pattern）落到不同资源；
- `work_iterations` 参与键，因为重活轮数不同意味着成果不同。

键相同即视为「同一份准备工作」。

### 3.2 owner / waiter / 复用 三种角色

执行器收到 `POST /execute` 后：

1. 先做短临界区登记（占用工具位、写入状态），随即释放全局账本锁——文件检查与进程启动都不在锁内。
2. 校验暂存工作区（输入版本、租户、文件身份）。
3. 查共享存储：命中且未过期 → 直接复用成果，角色 `shared`，不启动任何进程。
4. 未命中则向协调者申领资源键：
   - 申领成功 → 角色 `owner`，真实启动工具进程；
   - 申领失败 → 角色 `waiter`，不启动进程，等待 owner 的成果。

等待者拿到成果后按自己的 attempt 重新写产物，`artifact_id` 与 `artifact_sha256` 各自独立。

### 3.3 取消与失败

- 等待者可以单独取消：它只是退出等待，owner 继续跑，成果照常发布；
- 拥有者负责共享工作的结束与回收：成果发布后等待者被唤醒，等待者各自拿一份；
- 失败按明确规则重新申请：owner 失败时协调者把该键标记为可再次申领，等待者收到失败后可以重新申领并真正执行。

### 3.4 访问许可

协调者为每次工具执行签发一张许可（HMAC-SHA256 签名，密钥保存在 profile 目录，权限 0600）：

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
- 执行期间持有文件租约（`fcntl.flock`）：读模式 `LOCK_SH`（多读者并行），写模式 `LOCK_EX`（写独占）；
- 多资源按路径排序后依次取锁，因此反向请求不会互锁；
- 发布产物前再次核对输入版本，防止执行期间暂存内容被改动。

### 3.6 记账

协调者在 `access/ledger.jsonl` 追加一行行记录：`permit_issued`、`lock_wait`（含等待毫秒与排序后的键）、
`shared_published`（拥有者与共享次数）、`shared_failed`。

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
| P1 `await_outcome` 丢失唤醒：`publish`/`fail` 解析旧 future 后又补一个全新的未解析 future，且 `wait()` 挂起前不查已发布结果，错过调度间隙的 waiter 永久挂起（`service.py:872` 轮询随之死循环） | `execution/access.py`：`wait()` 先查 `lookup` 与新增的失败记录 `_failed`，均未命中才注册 future，检查与注册之间无 await 间隙；`publish`/`fail` 只 pop 并解析已有 future，不再补空 future；`claim` 清除历史失败记录 | `tests/unit/test_execution_access.py::test_await_outcome_after_publish_is_served_immediately`、`::test_await_outcome_after_fail_raises_immediately` |
| P1 `_watch` 成功路径先置 `quiescent` 后置 `outcome`：两次赋值之间有 `await access.publish`（写锁+原子写）的真实窗口，`service.py:872` 轮询观察到 quiescent=True + PENDING 即误报 TOOL_PENDING | `adapters/executor.py` `_watch()`：`state.quiescent` 移到全部终态字段落定之后、同一同步块内置位；except 分支改用局部变量 `quiescent` 判定 FAILED/UNKNOWN，不读半更新状态 | `tests/integration/test_shared_access.py::test_settling_never_shows_quiescent_pending_to_poller`（SlowPublish 子类把 publish 加宽 0.3s，按 service 方式轮询，断言从不观察到脏中间态，最终 SUCCEEDED 且 role=owner） |
| P2 claim 成功后 `_launch` 抛异常（`cgroups.create`/`issue_permit`/spec 落盘）不通知协调者，键滞留 in-flight，此后同键 run() 全部沦为 waiter 永久等待 | `adapters/executor.py` `run()`：新增 `claimed` 标志，异常路径对已 claim 的键调用 `access.fail(key, "类型名: 消息")` 再抛；`_launch` 的 Popen OSError 分支自行上报且不抛异常，`fail()` 幂等，不会重复失败 | `tests/integration/test_shared_access.py::test_launch_failure_frees_the_claim_for_the_next_attempt`（断言恰好一条 shared_failed 账本记录，随后同键 run() 成为 owner 并成功） |

缺陷实证（临时脚本，不入库）：

- prove1.py（macOS 宿主，Python 3.12.13）：claim→publish→await_outcome。
  修复前输出 `BUG STILL PRESENT: await_outcome hangs after publish`；
  修复后输出 `SERVED: 39f94024cbcf740958d8bb6e25095e33ee260776744698ff4e0d43a4454bf72f`。
- prove2.py（Linux 容器，SlowPublish 加宽 publish 窗口 0.3s，复刻 `service.py:872` 轮询）：
  修复前（对 f570222 原始树）输出 `DIRTY MIDSTATE (bug) PENDING`；
  修复后输出 `CLEAN SUCCEEDED`。

验证（head = 本修复 commit 的工作树，环境与上文相同的 Linux 容器 Python 3.12.15，
`uv sync --locked --group dev`，命令与 §5 相同）：

| 项目 | 结果 |
| --- | --- |
| pytest tests/unit tests/integration tests/l4 | 345 passed, 1 skipped in 162.78s（基线 341 + 新增 4 例回归，无回退） |
| pytest tests/smoke | 6 passed in 55.48s |
| ruff format --check | 207 files already formatted |
| ruff check | All checks passed! |
| mypy src | Success: no issues found in 53 source files |
| lint-imports | Contracts: 3 kept, 0 broken. |

## 6. 已知边界

- 共享判定以内容摘要为准：两份内容完全相同但文件名不同的资料会被视为不同资源，不共享。
- 共享成果只在协调者的保留期内有效（默认 600 秒），过期后重新执行。
- 跨进程互斥依赖 POSIX 文件锁，仅在 Linux 目标平台可用；macOS 上工具执行本身不可运行（`/proc` 依赖）。
- 许可校验发生在工具子进程入口，不替代执行器侧的输入版本与租户校验，两者同时生效。
- 等待者不启动进程，但占用工具槽：槽位统计按非 quiescent 状态计数（`executor.py:129`），等待者在等到成果前一直计入占用。
