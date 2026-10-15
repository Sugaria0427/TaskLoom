import socket

import pytest

from taskloom.bootstrap.development import Topology


def test_busy_port_is_reported_before_any_service_starts(tmp_path) -> None:
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        topology = Topology(tmp_path / "busy", client_port=port)
        with pytest.raises(RuntimeError, match=f"Port {port} is already in use"):
            topology.start()
        assert not topology.children
        assert not (topology.base / "dev.json").exists()
