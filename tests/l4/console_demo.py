"""Live demo of the platform scheduler console: a real platform service fed a replayed workload.

uv run --locked python tests/l4/console_demo.py                 # Aether trace if present, else synthetic
uv run --locked python tests/l4/console_demo.py --synthetic     # never read the trace
uv run --locked python tests/l4/console_demo.py --speedup 3 --capacity 3 --policy hybrid

Then open http://127.0.0.1:8896/admin and paste the admin key printed at start.

Workload: the busiest 30-minute window of .runtime/l4bench/traces/aether.csv (hashed ids and
numbers only, from `bench.py export-traces`). Each traced user becomes a platform account,
each hashed API key a root task; arrivals, service times, output tokens and cache share are
the trace's own, compressed --speedup times and looped. Prompts are scaled down to fit the
platform's per-request limits. Without the trace file a synthetic workload with the trace's
shape (one heavy user, a long light tail) is generated instead.

The platform is the real `platform_app` with a SQLite store under .runtime/console-demo/; the
model pool is a stand-in in this process that answers after the traced service time with the
traced usage. Nothing here needs Linux, the client or the tool executor.
"""

import argparse
import asyncio
import bisect
import math
import random
import secrets
import shutil
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, get_args

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from l4bench import traces

from taskloom.bootstrap.apps import platform_app
from taskloom.contracts.models import PlatformConfig, PoolAccount, mock_tokens
from taskloom.policies.scheduling import PolicyName

STATE = traces.DATA_DIR.parent / "console-demo"
WINDOW = 1800.0
PROMPT_SCALE, PROMPT_CAP, OUTPUT_CAP = 100, 8000, 4096


def busiest_window(rows: list[traces.TraceRequest]) -> list[traces.TraceRequest]:
    """The 30-minute window with the most active users (>= 10 requests), then most load."""
    done = sorted((r for r in rows if r.status == "completed" and r.input_tokens > 0),
                  key=lambda r: r.start)  # fmt: skip
    starts = [r.start for r in done]
    best: tuple[int, float, int, int] = (0, 0.0, 0, 0)
    for a in range(0, len(done), 25):
        b = bisect.bisect_left(starts, starts[a] + WINDOW)
        users = Counter(r.user for r in done[a:b])
        active = sum(count >= 10 for count in users.values())
        load = sum(r.service for r in done[a:b]) / WINDOW
        # A pool of a few slots should be contended, not swamped.
        if load <= 10 and (active, load) > best[:2]:
            best = (active, load, a, b)
    return done[best[2] : best[3]]


def from_trace(path: Path) -> list[dict[str, Any]]:
    window = busiest_window(traces.load(path))
    origin = window[0].start
    return [
        {
            "user": "u" + r.user[:8],
            "root": "key-" + r.key[:8],
            "agent": r.key,
            "offset": r.start - origin,
            "service": r.service,
            "prompt": max(16, min(PROMPT_CAP, r.input_tokens // PROMPT_SCALE)),
            "cached_share": r.cached_tokens / r.input_tokens,
            "output": max(1, min(OUTPUT_CAP, r.output_tokens)),
        }
        for r in window
    ]


def synthetic(seed: int = 7) -> list[dict[str, Any]]:
    """About 200 requests in 30 minutes from six users with the trace's skew and sizes."""
    rng = random.Random(seed)
    weights = {"heavy": 0.34, "steady": 0.24, "coder": 0.16, "light": 0.12, "casual": 0.08,
               "rare": 0.06}  # fmt: skip
    rows = []
    offset = 0.0
    while offset < WINDOW:
        offset += rng.expovariate(200 / WINDOW)
        user = rng.choices(list(weights), list(weights.values()))[0]
        output = max(1, min(OUTPUT_CAP, int(rng.lognormvariate(math.log(330), 1.2))))
        rows.append(
            {
                "user": user,
                "root": f"{user}-{rng.randrange(2)}",
                "agent": f"{user}-agent",
                "offset": offset,
                "service": traces.BASE_SECONDS + traces.SECONDS_PER_OUTPUT_TOKEN * output,
                "prompt": max(16, min(PROMPT_CAP, int(rng.lognormvariate(math.log(1550), 1.0)))),
                "cached_share": 0.9,
                "output": output,
            }
        )
    return rows


def replay_pool(rows: list[dict[str, Any]], key: str, capacity: int, speedup: float) -> FastAPI:
    """An OpenAI-style pool that answers each request as its trace row did."""
    app = FastAPI()
    replies: dict[str, dict[str, Any]] = {}
    active = 0

    @app.post("/v1/chat/completions")
    async def completion(
        body: dict[str, Any],
        authorization: Annotated[str, Header()] = "",
        request_id: Annotated[str, Header(alias="X-TaskLoom-Request-Id")] = "",
    ) -> dict[str, Any]:
        nonlocal active
        if not secrets.compare_digest(authorization, "Bearer " + key):
            raise HTTPException(401)
        if request_id in replies:
            return replies[request_id]
        if active >= capacity:
            raise HTTPException(429, "POOL_BUSY")
        row = rows[int(body["messages"][0]["content"].split("]", 1)[0].removeprefix("[row "))]
        active += 1
        try:
            await asyncio.sleep(row["service"] / speedup)
        finally:
            active -= 1
        # The gateway's own prompt count, so its hard bounds hold (design doc §6).
        prompt = mock_tokens({"messages": body["messages"], "tools": body.get("tools", [])})
        reply = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": min(row["output"], int(body["max_tokens"])),
                "prompt_tokens_details": {"cached_tokens": int(prompt * row["cached_share"])},
            },
        }
        replies[request_id] = reply
        while len(replies) > 4096:
            replies.pop(next(iter(replies)))
        return reply

    @app.get("/requests/{request_id}")
    async def query(request_id: str) -> dict[str, Any]:
        if request_id not in replies:
            raise HTTPException(404)
        return {"response": replies[request_id]}

    return app


async def drive(platform: str, rows: list[dict[str, Any]], speedup: float) -> None:
    keys: dict[str, str] = {}
    async with httpx.AsyncClient(base_url=platform, timeout=15) as http:
        for user in sorted({r["user"] for r in rows}):
            login = {"username": user, "password": "console-demo-password"}
            reply = await http.post("/v1/accounts/register", json=login)
            if reply.status_code != 201:
                reply = await http.post("/v1/accounts/login", json=login)
            keys[user] = reply.json()["api_key"]
        for user, root in {(r["user"], r["root"]) for r in rows}:
            await http.post("/v1/roots", json={"root_task_id": root, "budget_units": 10_000_000},
                            headers={"Authorization": "Bearer " + keys[user]})  # fmt: skip
        lap = 0
        while True:
            began = time.monotonic()
            for index, row in enumerate(rows):
                delay = began + row["offset"] / speedup - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                # mock_tokens counts JSON bytes / 4, so the filler sizes the scaled prompt.
                filler = "x" * max(0, row["prompt"] * 4 - 40)
                body = {
                    "request_id": f"l{lap}-{index}",
                    "root_task_id": row["root"],
                    "agent_id": row["agent"],
                    "step_id": f"s{index}",
                    "attempt_id": f"a{lap}-{index}",
                    "messages": [{"role": "user", "content": f"[row {index}] {filler}"}],
                    "max_tokens": min(OUTPUT_CAP, max(256, 2 * row["output"])),
                    "deadline_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                }
                await http.post("/v1/requests", json=body,
                                headers={"Authorization": "Bearer " + keys[row["user"]]})  # fmt: skip
            lap += 1


async def main(args: argparse.Namespace) -> None:
    trace = traces.TRACE_DIR / "aether.csv"
    if args.synthetic or not trace.exists():
        rows = synthetic()
        source = "synthetic workload" + (
            " (--synthetic)" if args.synthetic else f" ({trace} not found)"
        )
    else:
        rows, source = from_trace(trace), f"busiest window of {trace}"
    if args.reset and STATE.exists():
        shutil.rmtree(STATE)
    STATE.mkdir(parents=True, exist_ok=True)
    key_file = STATE / "admin_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(16))
        key_file.chmod(0o600)
    pool_key = secrets.token_urlsafe(24)
    half = args.capacity // 2
    platform = platform_app(STATE, PlatformConfig(
        pool_url=f"http://127.0.0.1:{args.pool_port}", pool_key=pool_key,
        pool_accounts=[PoolAccount(account_id="upstream-a", capacity=args.capacity - half),
                       PoolAccount(account_id="upstream-b", capacity=half)]
        if half else [],
        pool_capacity=args.capacity, scheduling_policy=args.policy,
        account_max_inflight=2, account_tokens_per_window=60000, tokens_per_window=200000,
        requests_per_window=600, account_requests_per_window=200,
        output_price=4.0, cache_read_price=0.1, cache_write_price=1.25,
        max_queue=256, max_tenant_queue=64, admin_key=key_file.read_text().strip(),
    ))  # fmt: skip
    servers = [
        uvicorn.Server(uvicorn.Config(replay_pool(rows, pool_key, args.capacity, args.speedup),
                                      host="127.0.0.1", port=args.pool_port, log_level="warning")),
        uvicorn.Server(uvicorn.Config(platform, host=args.host, port=args.port,
                                      log_level="warning")),
    ]  # fmt: skip
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    await asyncio.sleep(1.5)
    users = len({r["user"] for r in rows})
    print(
        f"workload: {source}\n  {len(rows)} requests, {users} users, "
        f"{WINDOW / args.speedup:.0f} s per lap, looped"
    )
    print(f"console:   http://127.0.0.1:{args.port}/admin")
    print(f"admin key: {key_file.read_text().strip()}  (also in {key_file})", flush=True)
    tasks.append(asyncio.create_task(drive(f"http://127.0.0.1:{args.port}", rows, args.speedup)))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--synthetic", action="store_true", help="ignore the local trace")
    parser.add_argument("--speedup", type=float, default=6.0, help="trace time compression")
    parser.add_argument(
        "--capacity", type=int, default=4, help="pool slots (split over 2 accounts)"
    )
    parser.add_argument(
        "--policy",
        default="rr",
        choices=get_args(PolicyName),
        help="starting policy; a console change persists over this",
    )
    parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to reach it from elsewhere")
    parser.add_argument("--port", type=int, default=8896)
    parser.add_argument("--pool-port", type=int, default=8897)
    parser.add_argument("--reset", action="store_true", help="start from an empty platform state")
    try:
        asyncio.run(main(parser.parse_args()))
    except KeyboardInterrupt:
        pass
