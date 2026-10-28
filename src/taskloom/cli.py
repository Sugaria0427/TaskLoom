import argparse
import json
import os
from pathlib import Path

import uvicorn

from taskloom import package_version
from taskloom.adapters.linux import executor_socket
from taskloom.adapters.supervisor import stop_supervisor, supervise
from taskloom.bootstrap.apps import client_app, executor_app, platform_app, pool_app
from taskloom.bootstrap.config import load_config, load_upstream
from taskloom.bootstrap.development import WP6_CAPACITIES, develop, self_test, stop_development
from taskloom.contracts.models import ClientConfig, PlatformConfig, PoolConfig, digest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TaskLoom unified personal client and model platform"
    )
    parser.add_argument("--version", action="version", version=package_version())
    parser.add_argument(
        "role",
        choices=[
            "client",
            "executor",
            "supervisor",
            "stop-client",
            "platform",
            "model-pool",
            "dev",
            "stop",
            "self-test",
        ],
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--port", type=int)
    parser.add_argument("--platform-port", "--gateway-port", dest="platform_port", type=int)
    parser.add_argument("--pool-port", "--provider-port", dest="pool_port", type=int)
    parser.add_argument("--second-client-port", type=int)
    parser.add_argument("--single-client", action="store_true")
    parser.add_argument(
        "--scenario",
        choices=sorted(WP6_CAPACITIES),
        help="WP6 capacity scenario (default: unconstrained)",
    )
    parser.add_argument("--tool-slots", type=int, help="Expected client tool slots for scenario")
    parser.add_argument(
        "--pool-capacity", type=int, help="Expected model pool capacity for scenario"
    )
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--upstream-config", type=Path, help="Private upstream JSON, dev only")
    args = parser.parse_args()
    if args.upstream_config is not None and args.role not in ("dev", "stop"):
        parser.error("--upstream-config is supported by dev and stop")
    if args.role != "dev" and any(
        value is not None for value in (args.scenario, args.tool_slots, args.pool_capacity)
    ):
        parser.error("WP6 scenario and capacity flags are only supported by dev")
    base = args.state_dir.expanduser().resolve()
    if args.upstream_config is not None:
        try:
            upstream = load_upstream(args.upstream_config)
        except ValueError as error:
            raise SystemExit(str(error)) from None
        # Keep provider/model histories, obligations and usage units in separate ledgers.
        namespace = digest({"url": upstream.base_url, "model": upstream.model})[:12]
        base = base / "upstreams" / namespace
    base.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    if args.role == "dev":
        try:
            develop(
                base,
                args.port,
                args.platform_port,
                args.pool_port,
                args.second_client_port,
                not args.single_client,
                scenario=args.scenario or "unconstrained",
                tool_slots=args.tool_slots,
                pool_capacity=args.pool_capacity,
                upstream_config=args.upstream_config,
            )
        except (RuntimeError, ValueError) as error:
            raise SystemExit(str(error)) from None
    elif args.role == "stop":
        stop_development(base)
    elif args.role == "self-test":
        print(json.dumps(self_test(base), ensure_ascii=False))
    elif args.role == "platform":
        platform = load_config(PlatformConfig, args.config)
        uvicorn.run(
            platform_app(base, platform),
            host=platform.listen_host,
            port=args.port or 8796,
            log_level="warning",
        )
    elif args.role == "model-pool":
        pool = load_config(PoolConfig, args.config)
        uvicorn.run(
            pool_app(base, pool), host="127.0.0.1", port=args.port or 8797, log_level="warning"
        )
    else:
        client = load_config(
            ClientConfig, args.config, {"profile": args.profile, "client_port": args.port}
        )
        if args.role == "stop-client":
            if not stop_supervisor(base / "clients" / client.profile):
                raise SystemExit("The personal client is still stopping; inspect its profile logs")
            return
        if args.role == "supervisor":
            supervise(base, client, args.daemon)
        elif args.role == "client":
            uvicorn.run(
                client_app(base, client),
                host="127.0.0.1",
                port=args.port or client.client_port,
                log_level="warning",
            )
        else:
            socket = executor_socket(base / "clients" / client.profile)
            socket.parent.mkdir(parents=True, exist_ok=True)
            uvicorn.run(executor_app(base, client), uds=str(socket), log_level="warning")
