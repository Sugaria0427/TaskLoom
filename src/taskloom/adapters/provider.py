import json
from typing import Any

import httpx

from taskloom.contracts.models import ModelReply, ModelRequest, ToolCall, Usage


class HTTPProvider:
    def __init__(self, base_url: str, model: str, key: str = "", kind: str = "simulated") -> None:
        self.kind = kind
        self.model = model
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(30, connect=3),
            headers={"Authorization": f"Bearer {key}"} if key else {},
            trust_env=False,
        )

    async def generate(self, request: ModelRequest) -> ModelReply:
        response = await self.client.post(
            "/v1/chat/completions",
            json={
                "model": self.model,
                "messages": request.messages,
                "tools": request.tools,
                "max_tokens": request.max_tokens,
            },
            headers={"X-TaskLoom-Request-Id": request.request_id},
        )
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

    async def reconcile(self, request_id: str) -> ModelReply | None:
        if self.kind != "simulated":
            return None
        response = await self.client.get(f"/requests/{request_id}")
        if response.status_code != 200:
            return None
        body = response.json()
        return self._parse(body["response"]) if body.get("response") else None

    async def close(self) -> None:
        await self.client.aclose()
