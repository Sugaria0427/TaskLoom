import httpx

from taskloom.bootstrap.apps import platform_app
from taskloom.contracts.models import PlatformConfig


async def test_authenticated_root_and_bounded_body(tmp_path) -> None:
    app = platform_app(tmp_path, PlatformConfig())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            issued = await client.post(
                "/v1/accounts/register", json={"username": "alice", "password": "test-password-123"}
            )
            assert issued.status_code == 201
            token = issued.json()["api_key"]
            denied = await client.post("/v1/roots", json={"root_task_id": "root"})
            assert denied.status_code == 401
            created = await client.post(
                "/v1/roots",
                json={"root_task_id": "root"},
                headers={"Authorization": "Bearer " + token},
            )
            assert created.status_code == 202
            hidden = await client.get(
                "/v1/requests/other-user", headers={"Authorization": "Bearer invalid-key"}
            )
            assert hidden.status_code == 401
            large = await client.post(
                "/v1/requests",
                content=b"x" * 1048577,
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
            )
            assert large.status_code == 413
            assert (await client.get("/health/ready")).status_code == 200
