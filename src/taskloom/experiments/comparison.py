"""Bounded, paired scheduling experiments using the production policy components.

Only the service times and sequential model/tool workload are simulated. No HTTP request,
token billing, OS process, quality verdict or real-model improvement is fabricated here.
"""

import heapq
import math
import random
import statistics
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Literal

from pydantic import Field, model_validator

from taskloom.contracts.models import Contract, digest
from taskloom.policies.aging import AgingQueue
from taskloom.policies.scheduling import scheduling_policy

Policy = Literal["fifo", "round_robin", "taskloom"]
POLICIES: tuple[Policy, ...] = ("fifo", "round_robin", "taskloom")
DAY = 86400
TRACE_LIMIT = 360


class ComparisonSpec(Contract):
    days: int = Field(default=7, ge=1, le=30)
    tasks_per_day: int = Field(default=60, ge=8, le=200)
    users: int = Field(default=4, ge=2, le=8)
    tool_slots: int = Field(default=2, ge=1, le=4)
    memory_mib: int = Field(default=512, ge=384, le=2048)
    model_slots: int = Field(default=2, ge=1, le=8)
    local_model_slots: int = Field(default=2, ge=1, le=4)
    active_seconds_per_day: int = Field(default=300, ge=30, le=3600)
    seed: int = Field(default=42, ge=0, le=2147483647)
    repeats: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def bounded_work(self) -> "ComparisonSpec":
        if self.days * self.tasks_per_day * self.repeats > 18000:
            raise ValueError("Limit each experiment to 18000 generated tasks across seeds")
        return self


@dataclass(frozen=True)
class Stage:
    kind: str
    seconds: float
    memory_mib: int = 0


@dataclass(frozen=True)
class Task:
    key: str
    user: str
    category: str
    arrival: float
    deadline: float
    stages: tuple[Stage, ...]


def workload(spec: ComparisonSpec, seed: int) -> list[Task]:
    """Generate one immutable workload, shared by every policy in the paired run."""
    rng = random.Random(seed)
    tasks = []
    profiles = {
        "interactive": (1.0, 1.0, 2.0, 64, 60.0),
        "analysis": (4.0, 8.0, 10.0, 128, 300.0),
        "background": (8.0, 35.0, 60.0, 384, 1200.0),
    }
    for day in range(spec.days):
        for index in range(spec.tasks_per_day):
            category = rng.choices(list(profiles), weights=(0.60, 0.15, 0.25))[0]
            first, tool, final, memory, deadline = profiles[category]
            # Background bursts and interactive arrivals compete during the same window.
            spread = (
                min(20, spec.active_seconds_per_day)
                if category == "background"
                else spec.active_seconds_per_day
            )
            arrival = day * DAY + rng.uniform(0, spread)
            user = "user-0" if category == "background" else f"user-{rng.randrange(spec.users)}"
            scale = rng.uniform(0.8, 1.2)
            tasks.append(
                Task(
                    f"d{day}-t{index}",
                    user,
                    category,
                    arrival,
                    arrival + deadline,
                    (
                        Stage("model", first * scale),
                        Stage("tool", tool * scale, memory),
                        Stage("model", final * scale),
                    ),
                )
            )
    return sorted(tasks, key=lambda task: (task.arrival, task.key))


class _Queue:
    def __init__(self, run: "_Run", optimized: bool) -> None:
        self.run = run
        self.fifo: deque[str] = deque()
        self.pending: set[str] = set()
        self.fair = (
            AgingQueue[str](
                quantum=100,
                cost=lambda key: max(1, math.ceil(run.estimate(key) * 10)),
                deadline=lambda key: run.tasks[key].deadline,
                now=lambda: run.now,
                urgency_window=30,
                aging_window=60,
                urgency_burst=2,
                priority=lambda key: int(run.tasks[key].category == "interactive"),
            )
            if optimized
            else None
        )

    def push(self, key: str) -> None:
        self.pending.add(key)
        if self.fair is not None:
            self.fair.push(key, key)  # One root task owns every stage in its workflow.
        else:
            self.fifo.append(key)

    def pop(self, available: int | None = None) -> str | None:
        def fits(key: str) -> bool:
            return available is None or self.run.stage(key).memory_mib <= available

        if self.fair is None:
            selected = self.fifo.popleft() if self.fifo and fits(self.fifo[0]) else None
            if selected is not None:
                self.pending.remove(selected)
            return selected
        if available is not None and not any(fits(key) for key in self.pending):
            return None
        # A bounded number of DRR credit rounds; no wall-clock sleeping in a simulation.
        for _ in range(32):
            chosen = self.fair.pop(limit=1, accept=fits)
            if chosen:
                self.pending.remove(chosen[0])
                return chosen[0]
            if not len(self.fair):
                break
        return None

    def __len__(self) -> int:
        return len(self.fair) if self.fair is not None else len(self.fifo)


class _Run:
    def __init__(self, spec: ComparisonSpec, tasks: list[Task], policy: Policy) -> None:
        self.spec, self.policy = spec, policy
        self.tasks = {task.key: task for task in tasks}
        self.index = dict.fromkeys(self.tasks, 0)
        self.ready = {task.key: task.arrival for task in tasks}
        self.now = 0.0
        users = [f"user-{index}" for index in range(spec.users)]
        self.tools = {u: _Queue(self, policy == "taskloom") for u in users}
        self.models = {u: _Queue(self, policy == "taskloom") for u in users}
        self.tool_running = dict.fromkeys(users, 0)
        self.memory = dict.fromkeys(users, 0)
        self.local_models = dict.fromkeys(users, 0)
        self.platform = scheduling_policy("hybrid" if policy == "taskloom" else "rr")
        self.platform_fifo: deque[str] = deque()
        self.model_running = 0
        self.events: list[tuple[float, int, str, str]] = []
        self.sequence = 0
        self.trace: list[dict[str, Any]] = []
        self.trace_count = 0
        self.completions: dict[str, float] = {}
        self.waits = dict.fromkeys(self.tasks, 0.0)
        self.model_work = 0.0
        self.tool_work = 0.0
        self.peak_models = 0
        self.peak_memory = 0
        self.peak_tools = 0
        for task in tasks:
            self.event(task.arrival, "arrive", task.key)

    def stage(self, key: str) -> Stage:
        return self.tasks[key].stages[self.index[key]]

    def estimate(self, key: str) -> float:
        # Estimate from the declared task class; never reveal the random realized service
        # duration to a scheduling decision. The platform learns occupancy as work finishes.
        estimates = {
            "interactive": (1.0, 1.0, 2.0),
            "analysis": (4.0, 8.0, 10.0),
            "background": (8.0, 35.0, 60.0),
        }
        return estimates[self.tasks[key].category][self.index[key]]

    def event(self, at: float, kind: str, key: str) -> None:
        self.sequence += 1
        heapq.heappush(self.events, (at, self.sequence, kind, key))

    def note(self, kind: str, key: str) -> None:
        self.trace_count += 1
        if len(self.trace) < TRACE_LIMIT:
            task = self.tasks[key]
            self.trace.append(
                {
                    "at_seconds": round(self.now, 3),
                    "type": kind,
                    "task_id": key,
                    "user": task.user,
                    "category": task.category,
                    "stage_index": self.index[key],
                    "model_inflight": self.model_running,
                    "local_tool_inflight": self.tool_running[task.user],
                    "local_memory_mib": self.memory[task.user],
                }
            )

    def enqueue(self, key: str) -> None:
        task = self.tasks[key]
        self.ready[key] = self.now
        queues = self.models if self.stage(key).kind == "model" else self.tools
        queues[task.user].push(key)
        self.note(self.stage(key).kind + "_queued", key)

    def start(self, key: str) -> None:
        task, stage = self.tasks[key], self.stage(key)
        self.waits[key] += max(0.0, self.now - self.ready[key])
        if stage.kind == "model":
            self.model_running += 1
            self.model_work += stage.seconds
            self.peak_models = max(self.peak_models, self.model_running)
            self.platform.on_dispatch(
                key, task.user, task.key, self.now, math.ceil(self.estimate(key) * 10)
            )
        else:
            self.tool_running[task.user] += 1
            self.memory[task.user] += stage.memory_mib
            self.tool_work += stage.seconds
            self.peak_tools = max(self.peak_tools, self.tool_running[task.user])
            self.peak_memory = max(self.peak_memory, self.memory[task.user])
        self.note(stage.kind + "_started", key)
        self.event(self.now + stage.seconds, "finish", key)

    def finish(self, key: str) -> None:
        task, stage = self.tasks[key], self.stage(key)
        if stage.kind == "model":
            self.model_running -= 1
            self.local_models[task.user] -= 1
            self.platform.on_finish(key, self.now, math.ceil(stage.seconds * 10))
        else:
            self.tool_running[task.user] -= 1
            self.memory[task.user] -= stage.memory_mib
        self.note(stage.kind + "_finished", key)
        self.index[key] += 1
        if self.index[key] == len(task.stages):
            self.completions[key] = self.now
            self.note("task_finished", key)
        else:
            self.enqueue(key)

    def dispatch(self) -> None:
        for user, queue in self.tools.items():
            while self.tool_running[user] < self.spec.tool_slots:
                key = queue.pop(self.spec.memory_mib - self.memory[user])
                if key is None:
                    break
                self.start(key)
        for user, queue in self.models.items():
            while self.local_models[user] < self.spec.local_model_slots:
                key = queue.pop()
                if key is None:
                    break
                self.local_models[user] += 1
                if self.policy == "fifo":
                    self.platform_fifo.append(key)
                else:
                    self.platform.push(user, key, key, self.now)
        while self.model_running < self.spec.model_slots:
            key = (
                (self.platform_fifo.popleft() if self.platform_fifo else None)
                if self.policy == "fifo"
                else self.platform.pick(lambda _: True, self.now)
            )
            if key is None:
                break
            self.start(key)

    def run(self) -> dict[str, Any]:
        while self.events:
            self.now = self.events[0][0]
            while self.events and self.events[0][0] == self.now:
                _, _, kind, key = heapq.heappop(self.events)
                self.enqueue(key) if kind == "arrive" else self.finish(key)
            self.dispatch()
        if len(self.completions) != len(self.tasks):
            raise RuntimeError("Simulation left work stranded without a future event")
        rows: list[dict[str, Any]] = [
            {
                "task_id": key,
                "day": int(task.arrival // DAY) + 1,
                "category": task.category,
                "turnaround_seconds": self.completions[key] - task.arrival,
                "wait_seconds": self.waits[key],
                "on_time": self.completions[key] <= task.deadline,
            }
            for key, task in self.tasks.items()
        ]
        interactive = [r["turnaround_seconds"] for r in rows if r["category"] == "interactive"]
        background = [r["wait_seconds"] for r in rows if r["category"] == "background"]
        ordered = sorted(interactive)
        metrics = {
            "submitted_tasks": len(rows),
            "completed_tasks": len(rows),
            "on_time_rate": statistics.mean(r["on_time"] for r in rows),
            "mean_turnaround_seconds": statistics.mean(r["turnaround_seconds"] for r in rows),
            "interactive_mean_seconds": statistics.mean(interactive) if interactive else None,
            "interactive_p95_seconds": ordered[math.ceil(len(ordered) * 0.95) - 1]
            if ordered
            else None,
            "background_max_wait_seconds": max(background, default=0),
            "model_utilization": self.model_work
            / (max(self.now, self.spec.days * DAY) * self.spec.model_slots),
            "total_service_seconds": self.model_work + self.tool_work,
            "peak_model_slots": self.peak_models,
            "peak_tool_slots_per_user": self.peak_tools,
            "peak_memory_mib_per_user": self.peak_memory,
        }
        daily = []
        for day in range(1, self.spec.days + 1):
            items = [row for row in rows if row["day"] == day]
            quick = [row["turnaround_seconds"] for row in items if row["category"] == "interactive"]
            daily.append(
                {
                    "day": day,
                    "on_time_rate": statistics.mean(r["on_time"] for r in items),
                    "interactive_mean_seconds": statistics.mean(quick) if quick else None,
                }
            )
        return {
            "metrics": metrics,
            "daily": daily,
            "trace": self.trace,
            "trace_total_events": self.trace_count,
            "trace_truncated": self.trace_count > len(self.trace),
        }


def compare(spec: ComparisonSpec) -> dict[str, Any]:
    """Run paired seeds and report averages, dispersion and signed improvements."""
    runs: list[dict[str, Any]] = []
    for repeat in range(spec.repeats):
        seed = spec.seed + repeat
        tasks = workload(spec, seed)
        fingerprint = digest([asdict(task) for task in tasks])
        for policy in POLICIES:
            runs.append(
                {
                    "policy": policy,
                    "seed": seed,
                    "workload_hash": fingerprint,
                    **_Run(spec, tasks, policy).run(),
                }
            )
    summary: list[dict[str, Any]] = []
    for policy in POLICIES:
        selected = [run for run in runs if run["policy"] == policy]
        metrics: dict[str, Any] = {}
        for key in selected[0]["metrics"]:
            values = [run["metrics"][key] for run in selected if run["metrics"][key] is not None]
            metrics[key] = {
                "mean": statistics.mean(values) if values else None,
                "stddev": statistics.stdev(values) if len(values) > 1 else 0.0,
            }
        daily = [
            {
                "day": day,
                "interactive_mean_seconds": statistics.mean(values)
                if (
                    values := [
                        run["daily"][day - 1]["interactive_mean_seconds"]
                        for run in selected
                        if run["daily"][day - 1]["interactive_mean_seconds"] is not None
                    ]
                )
                else None,
            }
            for day in range(1, spec.days + 1)
        ]
        summary.append({"policy": policy, "metrics": metrics, "daily": daily})
    optimized = summary[-1]["metrics"]
    improvements: dict[str, Any] = {}
    for baseline in summary[:-1]:
        changes: dict[str, float | None] = {}
        for key in (
            "interactive_mean_seconds",
            "interactive_p95_seconds",
            "mean_turnaround_seconds",
            "background_max_wait_seconds",
        ):
            before, after = baseline["metrics"][key]["mean"], optimized[key]["mean"]
            changes[key] = 100 * (before - after) / before if before and after is not None else None
        changes["on_time_percentage_points"] = 100 * (
            optimized["on_time_rate"]["mean"] - baseline["metrics"]["on_time_rate"]["mean"]
        )
        improvements[baseline["policy"]] = changes
    return {
        "schema_version": 1,
        "model_version": "course-comparison-v1",
        "source": "discrete-event-simulation",
        "spec": spec.model_dump(),
        "assumptions": [
            "合成任务包含模型决策、本地工具、模型汇总三个顺序阶段；成本按任务类别估计，服务时长有独立随机波动。",
            "每个用户独立拥有本地工具、内存和模型请求名额，共享同一平台模型池。",
            "同一种子下三组使用完全相同的到达、任务、容量和服务时长；所有已到达工作均排空。",
            "复用生产 AgingQueue、成本 DRR 与平台 rr/hybrid；网络、故障、Token 窗口与反馈控制尚未模拟。",
            "按时完成衡量设定的仿真期限；真实输出质量由真实任务验收单独记录。",
        ],
        "policies": {
            "fifo": "本地与平台 FIFO",
            "round_robin": "本地 FIFO + 平台用户/根任务轮转",
            "taskloom": "本地成本公平、期限/老化与内存准入 + 平台 hybrid",
        },
        "summary": summary,
        "improvements_percent": improvements,
        "runs": runs,
        "trace_limit_per_run": TRACE_LIMIT,
    }
