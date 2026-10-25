"""Export actual TaskLoom client records for reproducible WP6 analysis."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import urlopen

from taskloom.experiments.capture import capture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", default="http://127.0.0.1:8795")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    base_url = args.client.rstrip("/") + "/"

    def fetch(path: str) -> object:
        with urlopen(urljoin(base_url, path.lstrip("/")), timeout=15) as response:
            return json.load(response)

    snapshot = capture(
        fetch,
        source=args.client.rstrip("/"),
        captured_at=datetime.now(UTC).isoformat(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    print(f"Captured {len(snapshot['tasks'])} tasks and {len(snapshot['events'])} events")


if __name__ == "__main__":
    main()
