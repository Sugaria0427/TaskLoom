import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

render_svg = runpy.run_path(str(Path(__file__).resolve().parents[2] / "tools" / "wp6_plot.py"))[
    "render_svg"
]


SCENARIOS = ("unconstrained", "tool_constrained", "api_constrained", "both_constrained")


def simulation() -> dict:
    return {
        "metadata": {
            "source": "simulation",
            "calibration_provenance": "synthetic-fixture",
            "calibration_source": None,
            "policy": "FIFO per stage; no B00/B10/B01/B11/B11+FB claims",
            "seed": 7,
            "days": 30,
            "arrivals_per_day": 24,
        },
        "summary": [
            {
                "source": "simulation",
                "scenario": scenario,
                "submitted_tasks": 720,
                "mean_tool_wait_seconds": float(index + 1),
                "mean_api_wait_seconds": float(index + 2),
            }
            for index, scenario in enumerate(SCENARIOS)
        ],
        "events": [],
    }


def test_svg_has_four_scenarios_values_and_explicit_simulation_provenance() -> None:
    svg = render_svg(simulation())
    assert 'data-scenario="both_constrained"' in svg
    assert 'data-series="tool" data-value="4"' in svg
    assert 'data-series="api" data-value="5"' in svg
    assert "Simulated FIFO" in svg
    assert "synthetic-fixture" in svg
    assert "Seed 7" in svg
    assert "B00" not in svg


def test_same_input_gives_identical_svg() -> None:
    result = simulation()
    assert render_svg(result) == render_svg(result)


@pytest.mark.parametrize("change", ["source", "missing", "duplicate", "negative", "bgroup"])
def test_invalid_or_unsupported_comparison_is_rejected(change: str) -> None:
    result = simulation()
    if change == "source":
        result["metadata"]["source"] = "measured"
    elif change == "missing":
        result["summary"].pop()
    elif change == "duplicate":
        result["summary"][-1]["scenario"] = "unconstrained"
    elif change == "negative":
        result["summary"][0]["mean_tool_wait_seconds"] = -1
    else:
        result["summary"][0]["scenario"] = "B11"
    with pytest.raises(ValueError):
        render_svg(result)


def test_cli_writes_svg(tmp_path: Path) -> None:
    source = tmp_path / "run.json"
    target = tmp_path / "chart.svg"
    source.write_text(json.dumps(simulation()), encoding="utf-8")
    script = Path(__file__).resolve().parents[2] / "tools" / "wp6_plot.py"
    subprocess.run(
        [sys.executable, str(script), "--input", str(source), "--output", str(target)], check=True
    )
    assert target.read_text(encoding="utf-8").startswith("<svg ")
