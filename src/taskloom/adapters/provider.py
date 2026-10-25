import json
import math
from collections.abc import Mapping
from typing import Any

import httpx

from taskloom.contracts.models import ModelReply, ModelRequest, ToolCall, Usage
from taskloom.gateway.ports import ProviderRejected
from taskloom.policies.reservation import Prices

# The one status that refuses at admission: a rate-limited or full pool answers 429 without
# running the request (the OpenAI-compatible contract, and the simulated pool's POOL_BUSY).
# A 503 or any other error may come from a proxy after the upstream ran, so it stays
# ambiguous and goes to reconciliation instead of being sent again.
REFUSED_STATUS = {429}


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
        prices: Prices | None = None,
    ) -> None:
        """`accounts` maps upstream account id -> (base_url, key); empty is a single account.

        `prices` must be the platform's: the gateway admits budgets on worst cases at these
        prices, so the charge reported here has to use them too.
        """
        self.kind = kind
        self.model = model
        self.prices = prices or Prices()
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
        return ModelReply(
            content=message.get("content") or "",
            tool_calls=calls,
            usage=Usage(
                input_tokens=usage["prompt_tokens"],
                output_tokens=usage["completion_tokens"],
                cost_units=self.charge(usage),
                unit="mock_microcredit" if self.kind == "simulated" else "token_unit",
            ),
        )

    def charge(self, usage: dict[str, Any]) -> int:
        """Cost in uncached-input-token units: cached prompt tokens at the read price, output
        at the output price; never above the gateway's worst case for the same tokens."""
        prompt, output = int(usage["prompt_tokens"]), int(usage["completion_tokens"])
        details = usage.get("prompt_tokens_details") or {}
        cached = min(prompt, int(details.get("cached_tokens") or 0))
        # The epsilon keeps float noise from rounding a whole unit up.
        return math.ceil(
            prompt - cached + cached * self.prices.cache_read + output * self.prices.output - 1e-9
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
