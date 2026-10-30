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
from taskloom.contracts.models import ClientConfig, UpstreamConfig


class AcceptanceTopology(Topology):
    """Enable phase tools only inside this disposable acceptance topology."""

    def client_config(self, profile: str, port: int) -> ClientConfig:
        config = super().client_config(profile, port)
        config.staged_tools = True
        return config


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
                    plan, _ = json.JSONDecoder().raw_decode(text.split("验收工具参数：", 1)[1])
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
    from runner import GROUPS, LIVE, LiveCase, PrerequisiteError, comparison, run_groups

    target = args.only or ("all" if args.case == "verify" else args.case)
    configured = copy.copy(args)
    configured.case = target
    if args.components_only and target in {*GROUPS, "all"}:
        return run_groups(configured, output)
    server = ThreadingHTTPServer(("127.0.0.1", free_port()), ScriptedModel)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cases = {}
    try:
        with tempfile.TemporaryDirectory(prefix="tl-accept-") as directory:
            topology = AcceptanceTopology(
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
                configured.url, configured.second_url = topology.client_url, topology.second_client_url
                configured.timeout = 90
                configured.no_browser, configured.file, configured.count = True, None, 3
                case = LiveCase(configured, output)
                try:
                    for base in (configured.url, configured.second_url):
                        case.api(base, "/api/v1/settings/demo", {
                            "enabled": True, "tools": True, "models": False,
                            "strength": 100, "text_reference_ms": 1000, "cap_seconds": 5,
                        })
                    if target in {*GROUPS, "all"}:
                        cases["groups"] = run_groups(configured, output)
                    elif target in LIVE:
                        try:
                            cases[target] = case.run()
                        except PrerequisiteError as error:
                            cases[target] = {"status": "NOT_READY", "reason": str(error)}
                    elif target != "compare":
                        results = {}
                        for name in CASES if target == "algorithms" else [target]:
                            value = CASES[name]()
                            results[name] = asyncio.run(value) if inspect.isawaitable(value) else value
                        cases["components"] = {"status": "PASSED", "results": results}
                    if target in {"all", "compare"}:
                        configured.days = args.days if target == "compare" else 1
                        cases["compare"] = comparison(configured)
                finally:
                    case.http.close()
            finally:
                topology.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    return {
        "case": "verify",
        "status": "FAILED" if any(case["status"] == "FAILED" for case in cases.values())
        else "PASSED" if all(case["status"] == "PASSED" for case in cases.values()) else "INCOMPLETE",
        "scope": "按OS算法分组的独立服务及实际工具进程；模型决策为免费脚本。打印的临时访问地址在命令结束后关闭。",
        "cases": cases,
    }
