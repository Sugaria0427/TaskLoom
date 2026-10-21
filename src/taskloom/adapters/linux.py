import fcntl
import hashlib
import json
import os
import signal
import time
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4


def executor_socket(profile: Path) -> Path:
    directory = Path("/tmp") / f"taskloom-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise RuntimeError("IPC directory ownership mismatch")
    directory.chmod(0o700)
    key = hashlib.sha256(str(profile.resolve()).encode()).hexdigest()[:24]
    return directory / (key + ".sock")


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def process_ticks(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        return text[text.rfind(")") + 2 :].split()[19]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return None


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def live_group(pgid: int, ticks: str | None, boot: str | None) -> list[int]:
    if boot != boot_id():
        return []
    current_ticks = process_ticks(pgid)
    if current_ticks is not None and ticks is not None and current_ticks != ticks:
        return []
    result: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "stat").read_text()
            fields = text[text.rfind(")") + 2 :].split()
            if int(fields[2]) == pgid and fields[0] not in ("Z", "X"):
                result.append(int(entry.name))
        except (OSError, ValueError, IndexError):
            continue
    return result


def stop_group(pgid: int, ticks: str | None, boot: str | None, grace: float = 0.5) -> bool:
    if not live_group(pgid, ticks, boot):
        return True
    for sig, duration in ((signal.SIGTERM, grace), (signal.SIGKILL, 1.5)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        end = time.monotonic() + duration
        while time.monotonic() < end:
            if not live_group(pgid, ticks, boot):
                return True
            time.sleep(0.02)
    return not live_group(pgid, ticks, boot)


class OwnerLock:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream: TextIO = path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.stream.close()
            raise RuntimeError(f"Another owner holds {path}") from None

    def close(self) -> None:
        fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()


class CgroupDomain:
    def __init__(self, root: Path | None) -> None:
        self.root = root.resolve() if root is not None else None
        if self.root is not None:
            if not self.root.is_relative_to(Path("/sys/fs/cgroup")):
                raise ValueError("Expected a delegated cgroup v2 directory")
            if not self.root.is_dir() or not os.access(self.root, os.W_OK):
                raise RuntimeError("The configured cgroup directory is not writable")

    def create(
        self, identity: str, memory: int, cpu_quota: int = 100000, cpu_period: int = 100000
    ) -> Path | None:
        if self.root is None:
            return None
        path = self.root / ("tl-" + identity)
        path.mkdir(exist_ok=True)
        for filename, value in (
            ("memory.max", str(memory)),
            ("memory.swap.max", "0"),
            ("memory.oom.group", "1"),
            ("pids.max", "32"),
            ("cpu.max", f"{cpu_quota} {cpu_period}"),
        ):
            (path / filename).write_text(value)
        return path

    def metrics(self, path: str) -> dict[str, int]:
        """Peak memory, OOM count and CPU time read before the group is removed."""
        group = Path(path)
        result: dict[str, int] = {}
        if self.root is None or not group.exists():
            return result
        try:
            result["peak_memory"] = int((group / "memory.peak").read_text().strip())
        except (OSError, ValueError):
            pass
        try:
            events = dict(
                line.split() for line in (group / "memory.events").read_text().splitlines()
            )
            result["oom_kill"] = int(events.get("oom_kill", 0))
        except (OSError, ValueError):
            pass
        try:
            cpu = dict(line.split() for line in (group / "cpu.stat").read_text().splitlines())
            result["cpu_usec"] = int(cpu.get("usage_usec", 0))
        except (OSError, ValueError):
            pass
        return result

    def stopped(self, path: str) -> bool:
        group = Path(path)
        if self.root is None or group.parent != self.root or not group.name.startswith("tl-"):
            raise ValueError("cgroup ownership mismatch")
        if not group.exists():
            return True
        return "populated 0" in (group / "cgroup.events").read_text()

    def cleanup(self, path: str) -> bool:
        group = Path(path)
        if not self.stopped(path):
            kill = group / "cgroup.kill"
            if kill.exists():
                kill.write_text("1")
            for _ in range(100):
                if self.stopped(path):
                    break
                time.sleep(0.02)
        stopped = self.stopped(path)
        if stopped and group.exists():
            group.rmdir()
        return stopped
