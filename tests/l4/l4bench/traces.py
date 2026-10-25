"""Private request traces, exported to local CSV with only numbers and hashed identities.

Sources are read-only: Aether's `usage` table over SSH (multi-user gateway arrivals, sizes and
latency) and an abyss capture database (Claude Code / Codex session chains). Raw ids are hashed
before they leave the source process, and no request or response text is ever selected.
"""

import csv
import hashlib
import io
import shlex
import sqlite3
import subprocess
from dataclasses import asdict, dataclass, fields
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[3] / ".runtime" / "l4bench"
TRACE_DIR = DATA_DIR / "traces"

# Latency fitted on Aether's three busiest models: 5.1-8.9 s + 20-30 ms per output token
# (R^2 0.80-0.94). Used where a source has no latency of its own.
BASE_SECONDS = 6.0
SECONDS_PER_OUTPUT_TOKEN = 0.025


@dataclass(frozen=True)
class TraceRequest:
    user: str
    key: str
    session: str  # empty when the source does not know it
    chain: str  # sub-agent within the session, empty for the main chain
    start: float  # unix seconds when the request was sent
    service: float  # seconds until the reply finished
    input_tokens: int  # whole prompt, cached part included
    cached_tokens: int
    cache_write_tokens: int
    output_tokens: int
    status: str
    model: str

    @property
    def end(self) -> float:
        return self.start + self.service


def save(path: Path, rows: list[TraceRequest]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, [f.name for f in fields(TraceRequest)])
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


def load(path: Path) -> list[TraceRequest]:
    with path.open(newline="") as handle:
        return [
            TraceRequest(
                user=row["user"],
                key=row["key"],
                session=row["session"],
                chain=row["chain"],
                start=float(row["start"]),
                service=float(row["service"]),
                input_tokens=int(row["input_tokens"]),
                cached_tokens=int(row["cached_tokens"]),
                cache_write_tokens=int(row["cache_write_tokens"]),
                output_tokens=int(row["output_tokens"]),
                status=row["status"],
                model=row["model"],
            )
            for row in csv.DictReader(handle)
        ]


def _hash(value: str | None) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12] if value else ""


# Hashing happens inside Postgres, so raw user and key ids never cross SSH.
AETHER_SQL = """
COPY (
  SELECT left(md5(user_id), 12), left(md5(coalesce(api_key_id, user_id)), 12),
         extract(epoch FROM created_at), coalesce(response_time_ms, 0),
         coalesce(input_tokens, 0), coalesce(cache_read_input_tokens, 0),
         coalesce(cache_creation_input_tokens, 0), coalesce(output_tokens, 0), status, model
  FROM usage WHERE user_id IS NOT NULL ORDER BY created_at
) TO STDOUT WITH CSV
"""


def export_aether(host: str = "pi", container: str = "aether-postgres") -> list[TraceRequest]:
    remote = f"docker exec {container} psql -U postgres -d aether -At -c " + shlex.quote(
        " ".join(AETHER_SQL.split())
    )
    output = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, remote],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    rows = []
    for user, key, at, ms, total, cached, written, out, status, model in csv.reader(
        io.StringIO(output)
    ):
        rows.append(
            TraceRequest(
                user, key, "", "", float(at), int(ms) / 1000, int(total), int(cached),
                int(written), int(out), status, model,
            )
        )  # fmt: skip
    return rows


ABYSS_SQL = """
SELECT q.user_id, q.device_context_id, q.session_id, q.subagent_id, r.observed_at,
       q.input_tokens, q.cache_read_tokens, q.cache_write_tokens, r.output_tokens, q.llm_model
FROM llm_usage_events q
JOIN llm_usage_events r ON r.response_id = q.response_id AND r.event_type = 'response'
WHERE q.event_type = 'request' AND q.session_id IS NOT NULL
ORDER BY r.observed_at
"""


def export_abyss(database: Path) -> list[TraceRequest]:
    """abyss stamps a request when its reply completes, so the start is back-dated by the fit."""
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        selected = connection.execute(ABYSS_SQL).fetchall()
    finally:
        connection.close()
    rows = []
    for (
        user,
        device,
        session,
        subagent,
        observed_us,
        fresh,
        cached,
        written,
        out,
        model,
    ) in selected:
        service = BASE_SECONDS + SECONDS_PER_OUTPUT_TOKEN * out
        rows.append(
            TraceRequest(
                _hash(user), _hash(device), _hash(session), _hash(subagent),
                observed_us / 1e6 - service, service, fresh + cached + written, cached, written,
                out, "completed", model,
            )
        )  # fmt: skip
    return rows
