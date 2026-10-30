"""Exercise the acceptance runner against a private topology and a scripted, free model."""

import asyncio
import copy
import hashlib
import inspect
import json
import tempfile
import threading
import time
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from components import CASES

from taskloom.bootstrap.development import Topology, free_port
from taskloom.contracts.models import UpstreamConfig


class ScriptedModel(BaseHTTPRequestHandler):
    """Only interpret the acceptance runner's declared tool plans, never natural language."""

    responses: dict[str, tuple[str, dict[str, Any]]] = {}
    guard = threading.Lock()

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        if self.headers.get("Authorization") != "Bearer acceptance-fixture":
            self.send_error(401)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 1048576:
            self.send_error(413)
            return
        raw = self.rfile.read(length)
        payload = json.loads(raw)
        key = self.headers.get("X-TaskLoom-Request-Id", "")
        hashed = hashlib.sha256(raw).hexdigest()
        with self.guard:
            previous = self.responses.get(key)
            if previous and previous[0] != hashed:
                self.send_error(409)
                return
            if previous:
                response = previous[1]
            else:
                messages = payload["messages"]
                if any(message.get("role") == "tool" for message in messages):
                    message = {
                        "role": "assistant",
                        "content": "已完成本次验收工具，结果以真实工具回执为准。",
                    }
                else:
                    text = next(
                        message["content"]
                        for message in reversed(messages)
                        if message.get("role") == "user"
                    )
                    plan = json.loads(text.split("验收工具参数：", 1)[1])
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_" + hashlib.sha256(key.encode()).hexdigest()[:16],
                                "type": "function",
                                "function": {
                                    "name": plan["tool"],
                                    "arguments": json.dumps(plan["arguments"]),
                                },
                            }
                        ],
                    }
                response = {
                    "id": key,
                    "choices": [{"message": message, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 32},
                }
                self.responses[key] = (hashed, response)
        time.sleep(0.12)
        data = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def verify(args: Namespace, output: Path) -> dict[str, Any]:
    from runner import LiveCase, comparison

    components = {}
    for name, function in CASES.items() if not args.only else []:
        value = function()
        components[name] = asyncio.run(value) if inspect.isawaitable(value) else value
        print("组件场景完成：" + name, flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", free_port()), ScriptedModel)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cases = {}
    try:
        with tempfile.TemporaryDirectory(prefix="tl-accept-") as directory:
            topology = Topology(
                Path(directory), ephemeral=True, with_second_client=True, scenario="api_constrained"
            )
            # The local fixture owns an HTTP loopback transport; no user upstream file is read.
            topology.upstream = UpstreamConfig.model_construct(
                base_url=f"http://127.0.0.1:{server.server_port}",
                api_key="acceptance-fixture",
                model="acceptance-scripted",
                thinking=None,
            )
            topology.start()
            try:
                print("隔离服务已启动：临时端口、临时账号、脚本模型；不调用 DeepSeek。", flush=True)
                for name in (
                    [args.only]
                    if args.only and args.only != "compare"
                    else []
                    if args.only
                    else ("read", "tools", "priority", "cancel", "platform")
                ):
                    configured = copy.copy(args)
                    configured.case, configured.url, configured.second_url = (
                        name,
                        topology.client_url,
                        topology.second_client_url,
                    )
                    configured.timeout = 90
                    configured.no_browser, configured.file, configured.count = True, None, 3
                    folder = output / name
                    folder.mkdir()
                    case = LiveCase(configured, folder)
                    try:
                        # Only this disposable topology is configured; user services stay untouched.
                        for base in (configured.url, configured.second_url):
                            case.api(
                                base,
                                "/api/v1/settings/demo",
                                {
                                    "enabled": True,
                                    "tools": True,
                                    "models": False,
                                    "strength": 100,
                                    "text_reference_ms": 1000,
                                    "cap_seconds": 5,
                                },
                            )
                        cases[name] = case.run()
                    finally:
                        case.http.close()
                if not args.only or args.only == "compare":
                    configured = copy.copy(args)
                    configured.url, configured.days, configured.no_browser = (
                        topology.client_url,
                        1,
                        True,
                    )
                    cases["compare"] = comparison(configured)
            finally:
                topology.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    return {
        "case": "verify",
        "status": "PASSED"
        if all(case["status"] == "PASSED" for case in cases.values())
        else "NOT_OBSERVED",
        "scope": "五个算法组件、独立平台/个人端/实际工具进程，以及既有三策略仿真；模型决策为确定性脚本，不使用真实 API。",
        "components": components,
        "cases": cases,
    }
