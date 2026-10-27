"""Completed fixture history must not exhaust the live request lock registry."""

import asyncio

import httpx

from taskloom.bootstrap.apps import pool_app
from taskloom.contracts.models import PoolConfig


async def test_pool_accepts_over_2048_identities_and_still_deduplicates(tmp_path) -> None:
    config = PoolConfig(key="test-pool-lock-secret", latency_seconds=0, capacity=4)
    app = pool_app(tmp_path, config)
    headers = {"Authorization": "Bearer " + config.key}
    body = {"model": config.model, "messages": [{"role": "user", "content": "hello"}]}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as http:
            original = None
            for index in range(2050):
                response = await http.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={"X-TaskLoom-Request-Id": f"request-{index}"},
                )
                assert response.status_code == 200, response.text
                if index == 0:
                    original = response.json()
            assert (await http.get("/requests/request-0")).json()["response"] == original
            replay = await http.post(
                "/v1/chat/completions",
                json=body,
                headers={"X-TaskLoom-Request-Id": "request-0"},
            )
            assert replay.status_code == 200 and replay.json() == original
            # Waiting duplicates must share the same lock until the last waiter leaves.
            replies = await asyncio.gather(
                *(
                    http.post(
                        "/v1/chat/completions",
                        json=body,
                        headers={"X-TaskLoom-Request-Id": "concurrent"},
                    )
                    for _ in range(3)
                )
            )
            assert all(r.status_code == 200 for r in replies)
            assert replies[0].json() == replies[1].json() == replies[2].json()
            changed = {**body, "messages": [{"role": "user", "content": "changed"}]}
            conflict = await http.post(
                "/v1/chat/completions",
                json=changed,
                headers={"X-TaskLoom-Request-Id": "request-0"},
            )
            assert conflict.status_code == 409
