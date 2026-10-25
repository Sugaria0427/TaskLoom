"""WP2 STEP 1 · 多工具工作流功能演示（需在 Linux 环境运行）。

启动一次性 dev 环境，提交一个任务，真实跑出
「模型与四种工具交替推进」的多工具链，
并把执行时间线与最终结果打印出来，直观体现验收第 1 条。

用法（仓库根目录）：
    python tools/demo_multi_tool.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import cast

import httpx

from taskloom.bootstrap.development import Topology

# 两份文档均包含 "the"，保证 grep("the") 有真实命中。
DOCUMENTS = {
    "notes.txt": "the quick brown fox",
    "data.txt": "jumps over the lazy dog",
}


def wait_task(http: httpx.Client, task_id: str, seconds: float = 30) -> dict[str, object]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        body = http.get(f"/api/v1/tasks/{task_id}").json()
        if body["outcome"] != "PENDING":
            return cast(dict[str, object], body)
        time.sleep(0.05)
    raise RuntimeError("任务未在期限内完成")


def main() -> None:
    base = Path.home() / ".cache/taskloom/demo-multi-tool"
    topology = Topology(base, ephemeral=True, with_second_client=False)
    topology.start()
    try:
        with httpx.Client(base_url=topology.client_url, timeout=10, trust_env=False) as http:
            created = http.post(
                "/api/v1/tasks",
                headers={"X-TaskLoom-Local": "1"},
                json={"documents": DOCUMENTS},
            )
            created.raise_for_status()
            task_id = created.json()["task_id"]
            print("=== WP2 STEP 1 · 多工具工作流功能演示 ===\n")
            print("提交文档: notes.txt(4词) + data.txt(5词)")
            print(
                'fixture 模型将依次调用: file_stats、grep("the")、list_files、top_words，然后汇总\n'
            )
            print(f"任务已提交: {task_id}")
            task = wait_task(http, task_id)
            graph = http.get(f"/api/v1/tasks/{task_id}/graph").json()
            timeline(graph, task)
            summary = json.dumps(task["result"], ensure_ascii=False)
            print(f"\n=== 最终结果 ===\n{summary}")
            print(
                "\n=== 验证点 ===\n"
                "1. 时间线中出现四类工具（file_stats、grep、list_files、top_words）且都有真实执行记录（attempts 带 execution_ref）\n"
                "2. 出现 5 次模型请求，4 次工具执行，各阶段依赖连续\n"
                "3. 最终汇总与已验证工具输出一致（未触发 RESULT_MISMATCH）"
            )
    finally:
        topology.close()


def timeline(graph: dict[str, object], task: dict[str, object]) -> None:
    steps: list[dict[str, object]] = cast(list[dict[str, object]], graph["steps"])
    attempts = {
        cast(str, row["attempt_id"]): row
        for row in cast(list[dict[str, object]], graph["attempts"])
    }
    print("\n=== 执行时间线（模型与已注册工具交替推进）===")
    for index, step in enumerate(steps, 1):
        kind = cast(str, step["kind"])
        attempt = attempts.get(cast(str, step["current_attempt_id"]), {})
        deps = ", ".join(cast(list[str], step["predecessors"])) or "无"
        if kind == "tool":
            detail = f" → {json.dumps(attempt.get('result'), ensure_ascii=False)}"
            if attempt.get("execution_ref"):
                pid = cast(dict[str, object], attempt["execution_ref"])["pid"]
                detail += f" [真实子进程 pid={pid}]"
        else:
            detail = ""
        print(
            f"[{index}] {kind:5s} {str(step['step_id']).split('-')[-1]:10s} (依赖: {deps}){detail}"
        )
    print(f"\n模型请求次数: {sum(s['kind'] == 'model' for s in steps)}")
    print(f"工具执行次数: {sum(s['kind'] == 'tool' for s in steps)}")


if __name__ == "__main__":
    main()
