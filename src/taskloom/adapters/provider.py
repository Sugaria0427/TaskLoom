import json
from collections.abc import Mapping
from typing import Any

import httpx

from taskloom.contracts.models import ModelReply, ModelRequest, ToolCall, Usage
from taskloom.gateway.ports import ProviderRejected

# Explicit refusals: rate limited or unavailable, answered without running the request.
REFUSED_STATUS = {429, 503}


def _client(base_url: str, key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=httpx.Timeout(30, connect=3),
        headers={"Authorization": f"Bearer {key}"} if key else {},
        trust_env=False,
    )


class HTTPProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        key: str = "",
        kind: str = "simulated",
        accounts: Mapping[str, tuple[str, str]] | None = None,
    ) -> None:
        """`accounts` maps upstream account id -> (base_url, key); empty is a single account."""
        self.kind = kind
        self.model = model
        self.client = _client(base_url, key)
        self.accounts = {
            name: _client(url, account_key) for name, (url, account_key) in (accounts or {}).items()
        }

    def _route(self, account: str | None) -> httpx.AsyncClient:
        if account is None:
            return self.client
        if account not in self.accounts:
            raise ValueError(f"Unknown upstream pool account {account!r}")
        return self.accounts[account]

    async def generate(self, request: ModelRequest, account: str | None = None) -> ModelReply:
        try:
            response = await self._route(account).post(
                "/v1/chat/completions",
                json={
                    "model": self.model,
                    "messages": request.messages,
                    "tools": request.tools,
                    "max_tokens": request.max_tokens,
                },
                headers={"X-TaskLoom-Request-Id": request.request_id},
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
            # No connection was established, so the request never left this process.
            raise ProviderRejected(type(error).__name__) from error
        if response.status_code in REFUSED_STATUS:
            raise ProviderRejected(f"pool refused with HTTP {response.status_code}")
        # Other failures (read timeouts, 5xx after sending) stay ambiguous for reconciliation.
        response.raise_for_status()
        return self._parse(response.json())

    def _parse(self, data: dict[str, Any]) -> ModelReply:
        message = data["choices"][0]["message"]
        calls = [
            ToolCall(
                call_id=call["id"],
                name=call["function"]["name"],
                arguments=json.loads(call["function"]["arguments"]),
            )
            for call in message.get("tool_calls", [])
        ]
        usage = data["usage"]
        count = usage["prompt_tokens"] + usage["completion_tokens"]
        return ModelReply(
            content=message.get("content") or "",
            tool_calls=calls,
            usage=Usage(
                input_tokens=usage["prompt_tokens"],
                output_tokens=usage["completion_tokens"],
                cost_units=count,
                unit="mock_microcredit" if self.kind == "simulated" else "token_unit",
            ),
        )

    async def reconcile(self, request_id: str, account: str | None = None) -> ModelReply | None:
        if self.kind != "simulated":
            return None
        response = await self._route(account).get(f"/requests/{request_id}")
        if response.status_code != 200:
            return None
        body = response.json()
        return self._parse(body["response"]) if body.get("response") else None

    async def close(self) -> None:
        for client in [self.client, *self.accounts.values()]:
            await client.aclose()
