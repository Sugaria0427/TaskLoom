import subprocess
import sys
from pathlib import Path

import pytest

from taskloom import cli
from taskloom.bootstrap.development import Topology


@pytest.mark.parametrize(
    ("scenario", "tool_slots", "pool_capacity"),
    [
        ("unconstrained", 2, 2),
        ("tool_constrained", 1, 2),
        ("api_constrained", 2, 1),
        ("both_constrained", 1, 1),
    ],
)
def test_wp6_scenario_has_expected_capacity(
    tmp_path, scenario: str, tool_slots: int, pool_capacity: int
) -> None:
    topology = Topology(tmp_path, scenario=scenario)
    assert topology.tool_slots == tool_slots
    assert topology.pool_capacity == pool_capacity
    assert topology.client_config("alice", 8795).tool_slots == tool_slots
    assert topology.pool_config().capacity == pool_capacity
    assert topology.platform_config().pool_capacity == pool_capacity


def test_default_topology_preserves_two_by_two(tmp_path) -> None:
    topology = Topology(tmp_path)
    assert (topology.tool_slots, topology.pool_capacity) == (2, 2)


@pytest.mark.parametrize(
    ("scenario", "tool_slots", "pool_capacity"),
    [
        ("unknown", None, None),
        ("tool_constrained", 2, None),
        ("api_constrained", None, 2),
        ("unconstrained", 1, 1),
    ],
)
def test_topology_rejects_unknown_or_mismatched_capacity(
    tmp_path, scenario: str, tool_slots: int | None, pool_capacity: int | None
) -> None:
    with pytest.raises(ValueError):
        Topology(
            tmp_path,
            scenario=scenario,
            tool_slots=tool_slots,
            pool_capacity=pool_capacity,
        )


def test_dev_cli_passes_wp6_capacity_flags(tmp_path, monkeypatch) -> None:
    seen = {}

    def fake_develop(*args, **kwargs) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(cli, "develop", fake_develop)
    monkeypatch.setattr(
        "sys.argv",
        [
            "taskloom",
            "dev",
            "--state-dir",
            str(tmp_path),
            "--scenario",
            "tool_constrained",
            "--tool-slots",
            "1",
            "--pool-capacity",
            "2",
        ],
    )
    cli.main()
    assert seen["scenario"] == "tool_constrained"
    assert seen["tool_slots"] == 1
    assert seen["pool_capacity"] == 2


def test_dev_cli_rejects_unknown_scenario(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["taskloom", "dev", "--state-dir", str(tmp_path), "--scenario", "unknown"],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_project_dev_help_documents_wp6_flags() -> None:
    project = Path(__file__).resolve().parents[2] / "tools/project.py"
    result = subprocess.run(
        [sys.executable, str(project), "dev", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    for value in ("--scenario", "--tool-slots", "--pool-capacity", "both_constrained"):
        assert value in result.stdout
