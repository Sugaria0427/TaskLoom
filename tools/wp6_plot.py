"""Render the four FIFO simulation conditions as a deterministic SVG.

Usage: python tools/wp6_plot.py --input simulation.json --output waiting.svg
This chart reports simulated waiting only. It does not compare TaskLoom strategies.
"""

from __future__ import annotations

import argparse
import json
import math
from html import escape
from pathlib import Path
from typing import Any

SCENARIOS = (
    ("unconstrained", "Unconstrained"),
    ("tool_constrained", "Tool constrained"),
    ("api_constrained", "API constrained"),
    ("both_constrained", "Both constrained"),
)


def _number(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{key} must be a finite number")
    if value < 0:
        raise ValueError(f"{key} must be nonnegative")
    return float(value)


def _validate(data: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    metadata = data.get("metadata")
    rows = data.get("summary")
    if not isinstance(metadata, dict) or not isinstance(rows, list):
        raise ValueError("simulation result requires metadata and summary")
    if metadata.get("source") != "simulation":
        raise ValueError("chart accepts simulation results only")
    if metadata.get("calibration_provenance") not in {"synthetic-fixture", "measured"}:
        raise ValueError("calibration provenance is required")
    if metadata["calibration_provenance"] == "measured" and not metadata.get("calibration_source"):
        raise ValueError("measured calibration requires a source")
    if not isinstance(metadata.get("policy"), str) or not metadata["policy"].startswith(
        "FIFO per stage; no "
    ):
        raise ValueError("only FIFO simulation without B-group claims is supported")
    if not isinstance(metadata.get("seed"), int) or isinstance(metadata["seed"], bool):
        raise ValueError("simulation seed is required")
    expected = {scenario for scenario, _ in SCENARIOS}
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("source") != "simulation":
            raise ValueError("summary rows must be simulation records")
        scenario = row.get("scenario")
        if scenario not in expected or scenario in found:
            raise ValueError("unexpected or duplicate scenario")
        _number(row, "mean_tool_wait_seconds")
        _number(row, "mean_api_wait_seconds")
        count = row.get("submitted_tasks")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("submitted_tasks must be positive")
        found[scenario] = row
    if set(found) != expected:
        raise ValueError("all four simulation scenarios are required")
    return metadata, [found[scenario] for scenario, _ in SCENARIOS]


def render_svg(data: dict[str, Any]) -> str:
    """Render mean tool and API waiting, keeping calibration provenance visible."""
    metadata, rows = _validate(data)
    maximum = max(
        1.0,
        *(
            _number(row, metric)
            for row in rows
            for metric in ("mean_tool_wait_seconds", "mean_api_wait_seconds")
        ),
    )
    scale = 220 / maximum
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="475" viewBox="0 0 960 475" role="img" aria-labelledby="title description">',
        '<title id="title">Simulated FIFO mean waiting by capacity condition</title>',
        '<desc id="description">Mean tool and API queue waiting in seconds across four simulated capacity conditions.</desc>',
        '<rect width="960" height="475" fill="#ffffff"/>',
        '<text x="60" y="43" font-size="23" font-family="sans-serif" fill="#172554">Simulated FIFO waiting</text>',
        '<text x="60" y="69" font-size="14" font-family="sans-serif" fill="#475569">Mean queue wait (seconds); identical seeded arrivals and work</text>',
        '<line x1="60" y1="335" x2="920" y2="335" stroke="#94a3b8"/>',
        '<rect x="675" y="38" width="14" height="14" fill="#2563eb"/>',
        '<text x="695" y="50" font-size="13" font-family="sans-serif">Tool</text>',
        '<rect x="755" y="38" width="14" height="14" fill="#f97316"/>',
        '<text x="775" y="50" font-size="13" font-family="sans-serif">API</text>',
    ]
    for index, ((scenario, label), row) in enumerate(zip(SCENARIOS, rows, strict=True)):
        center = 150 + index * 215
        for series, metric, color, offset in (
            ("tool", "mean_tool_wait_seconds", "#2563eb", -30),
            ("api", "mean_api_wait_seconds", "#f97316", 10),
        ):
            value = _number(row, metric)
            height = value * scale
            parts.append(
                f'<rect data-scenario="{scenario}" data-series="{series}" '
                f'data-value="{value:g}" x="{center + offset}" y="{335 - height:.3f}" '
                f'width="34" height="{height:.3f}" fill="{color}"/>'
            )
            parts.append(
                f'<text x="{center + offset + 17}" y="{max(91, 327 - height):.3f}" '
                f'text-anchor="middle" font-size="12" font-family="sans-serif">{value:g}</text>'
            )
        parts.append(
            f'<text x="{center}" y="360" text-anchor="middle" font-size="13" '
            f'font-family="sans-serif">{label}</text>'
        )
    provenance = escape(str(metadata["calibration_provenance"]))
    source = metadata.get("calibration_source")
    detail = f" · Calibration source: {escape(str(source))}" if source else ""
    parts.append(
        f'<text x="60" y="411" font-size="13" font-family="sans-serif" fill="#334155">'
        f"Simulation · FIFO per stage · Calibration: {provenance}{detail} · Seed {metadata['seed']}"
        "</text>"
    )
    parts.append(
        '<text x="60" y="434" font-size="12" font-family="sans-serif" fill="#64748b">'
        "Illustrative simulation; no TaskLoom strategy comparison or observed performance claim."
        "</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="wp6_simulate.py result JSON")
    parser.add_argument("--output", type=Path, required=True, help="output SVG")
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        parser.error("simulation JSON must be an object")
    svg = render_svg(data)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(svg, encoding="utf-8")


if __name__ == "__main__":
    main()
