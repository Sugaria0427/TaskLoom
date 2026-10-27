"""Replay must remain a real workload after restarting with a saved platform database."""

import httpx
import pytest
from console_demo import drive

from taskloom.bootstrap.apps import platform_app
from taskloom.contracts.models import PlatformConfig


class EndLap(Exception):
    pass


async def test_replay_restart_uses_new_identities_and_reports_rejections(tmp_path, monkeypatch):
    app = platform_app(tmp_path, PlatformConfig())
    identities = []
    rejected = False
    client_type = httpx.AsyncClient

    class ReplayClient(client_type):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.ASGITransport(app=app), **kwargs)

        async def post(self, url, **kwargs):
            if url == "/v1/requests" and rejected:
                return httpx.Response(429, request=httpx.Request("POST", "http://test" + url))
            response = await super().post(url, **kwargs)
            if url == "/v1/requests":
                assert response.status_code == 202, response.text
                identities.append((kwargs["json"]["request_id"], kwargs["json"]["attempt_id"]))
                raise EndLap
            return response

    rows = [
        {"user": "alice", "root": "root", "agent": "agent", "offset": 0, "prompt": 16, "output": 1}
    ]
    async with app.router.lifespan_context(app):
        monkeypatch.setattr(httpx, "AsyncClient", ReplayClient)
        for _ in range(2):
            with pytest.raises(EndLap):
                await drive("http://test", rows, 1)
        assert len({item[0] for item in identities}) == 2
        assert len({item[1] for item in identities}) == 2
        rejected = True
        with pytest.raises(httpx.HTTPStatusError):
            await drive("http://test", rows, 1)
