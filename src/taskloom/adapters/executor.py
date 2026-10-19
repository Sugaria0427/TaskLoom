import asyncio
import json
import subprocess
import sys
from pathlib import Path
from threading import Thread
from typing import BinaryIO

from taskloom.adapters.linux import (
    CgroupDomain,
    atomic_json,
    boot_id,
    live_group,
    process_ticks,
    stop_group,
)
from taskloom.contracts.models import ToolRun, ToolState, digest
from taskloom.domain.model import DomainError, Outcome


class ProcessExecutor:
    """Persistent launch intent + one owned process group per Attempt."""

    def __init__(self, profile_dir: Path, cgroups: CgroupDomain, slots: int = 2) -> None:
        self.directory = profile_dir
        self.cgroups = cgroups
        self.slots = slots
        self.states: dict[str, ToolState] = {}
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.watchers: dict[str, asyncio.Task[None]] = {}
        self.cancelled: dict[str, int] = {}
        self.lock = asyncio.Lock()
        (self.directory / "executions").mkdir(parents=True, exist_ok=True)
        (self.directory / "cancelled").mkdir(exist_ok=True)

    def _save(self, state: ToolState) -> None:
        atomic_json(
            self.directory / "executions" / (state.request.attempt_id + ".json"),
            state.model_dump(mode="json"),
        )

    async def start(self) -> None:
        for path in (self.directory / "cancelled").glob("*.json"):
            self.cancelled[path.stem] = json.loads(path.read_text())["generation"]
        for path in (self.directory / "executions").glob("*.json"):
            state = ToolState.model_validate_json(path.read_text())
            if not state.quiescent:
                stopped = await asyncio.to_thread(self._stop, state)
                state.quiescent = stopped
                state.outcome = Outcome.FAILED if stopped else Outcome.UNKNOWN
                state.error = (
                    "Executor restarted; prior execution reconciled"
                    if stopped
                    else "Launch ownership unresolved"
                )
                self._save(state)
            self.states[state.request.attempt_id] = state

    def _stop(self, state: ToolState) -> bool:
        stopped = False
        if state.pid is not None:
            stopped = stop_group(state.pid, state.start_ticks, state.boot_id)
        if state.cgroup_path:
            group = Path(state.cgroup_path)
            if not group.exists():
                return True
            if group.name != "tl-" + state.request.attempt_id:
                return False
            try:
                domain = (
                    self.cgroups
                    if group.parent == self.cgroups.root
                    else CgroupDomain(group.parent)
                )
                stopped = domain.cleanup(state.cgroup_path)
            except (OSError, ValueError, RuntimeError):
                return False
        return stopped

    async def run(self, request: ToolRun) -> ToolState:
        async with self.lock:
            previous = self.states.get(request.attempt_id)
            if previous is not None:
                if previous.request != request:
                    raise DomainError("ATTEMPT_CONFLICT", "Tool attempt changed")
                return previous.model_copy(deep=True)
            state = ToolState(request=request)
            if self.cancelled.get(request.attempt_id, -1) >= request.generation:
                state.outcome = Outcome.CANCELLED
                state.quiescent = True
                self.states[request.attempt_id] = state
                self._save(state)
                return state
            if sum(not item.quiescent for item in self.states.values()) >= self.slots:
                raise DomainError("EXECUTOR_FULL", "Executor capacity exhausted", True)
            workspace = self.directory / "tasks" / request.root_task_id
            manifest = workspace / "input.json"
            if (
                not manifest.exists()
                or json.loads(manifest.read_text())["input_version"] != request.input_version
            ):
                raise DomainError("INPUT_VERSION_MISMATCH", "Staged input version differs")
            owner = json.loads(manifest.read_text()).get("tenant_id")
            if owner is not None and owner != request.tenant_id:
                raise DomainError(
                    "UNAUTHORIZED", "Tool workspace belongs to another platform account"
                )
            input_paths = list((workspace / "inputs").iterdir())
            if any(path.is_symlink() or not path.is_file() for path in input_paths):
                raise DomainError("INPUT_VERSION_MISMATCH", "Unexpected staged input object")
            actual = {path.name: path.read_bytes().decode("utf-8") for path in input_paths}
            if digest(actual) != request.input_version:
                raise DomainError("INPUT_VERSION_MISMATCH", "Staged file contents changed")
            output_dir = workspace / "attempts" / request.attempt_id
            output_dir.mkdir(parents=True, exist_ok=True)
            group = self.cgroups.create(request.attempt_id, request.memory_bytes)
            state.cgroup_path = str(group) if group else None
            spec = {
                "workspace": str(workspace),
                "output": str(output_dir / "result.json"),
                "input_version": request.input_version,
                "work_iterations": request.work_iterations,
                "emit_bytes": request.emit_bytes,
                "cgroup_path": state.cgroup_path,
                "tool_name": request.tool_name,
                "arguments": request.arguments,
            }
            atomic_json(output_dir / "spec.json", spec)
            self.states[request.attempt_id] = state
            self._save(state)
            try:
                process = await asyncio.to_thread(
                    subprocess.Popen,
                    [
                        sys.executable,
                        "-m",
                        "taskloom.execution.tool",
                        "--spec",
                        str(output_dir / "spec.json"),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    cwd=workspace,
                )
            except OSError as error:
                if state.cgroup_path:
                    self.cgroups.cleanup(state.cgroup_path)
                state.outcome = Outcome.FAILED
                state.quiescent = True
                state.error = str(error)
                self._save(state)
                return state
            state.pid = process.pid
            state.start_ticks = process_ticks(process.pid)
            state.boot_id = boot_id()
            self.processes[request.attempt_id] = process
            self._save(state)
            assert process.stdout is not None and process.stderr is not None
            readers = [
                Thread(
                    target=self._drain,
                    args=(stream, output_dir / filename, state, attribute),
                    name=f"tool-{attribute}-{request.attempt_id[:8]}",
                    daemon=False,
                )
                for stream, filename, attribute in (
                    (process.stdout, "stdout.log", "stdout_bytes"),
                    (process.stderr, "stderr.log", "stderr_bytes"),
                )
            ]
            for reader in readers:
                reader.start()
            self.watchers[request.attempt_id] = asyncio.create_task(
                self._watch(state, process, readers, output_dir)
            )
            return state.model_copy(deep=True)

    @staticmethod
    def _drain(stream: BinaryIO, target: Path, state: ToolState, attribute: str) -> None:
        total = 0
        kept = 0
        with stream, target.open("wb") as output:
            while chunk := stream.read(8192):
                total += len(chunk)
                allowance = max(0, 1048576 - kept)
                if allowance:
                    output.write(chunk[:allowance])
                    kept += min(allowance, len(chunk))
                if kept < total:
                    state.output_truncated = True
                setattr(state, attribute, total)

    async def _watch(
        self,
        state: ToolState,
        process: subprocess.Popen[bytes],
        readers: list[Thread],
        output: Path,
    ) -> None:
        try:
            try:
                code = await asyncio.wait_for(
                    asyncio.to_thread(process.wait), state.request.timeout_seconds
                )
            except TimeoutError:
                await asyncio.to_thread(self._stop, state)
                code = await asyncio.to_thread(process.wait)
                state.error = "TOOL_TIMEOUT"
            # A tool's root exiting does not prove its descendants exited.
            if state.pid is not None and live_group(state.pid, state.start_ticks, state.boot_id):
                await asyncio.to_thread(self._stop, state)
            for reader in readers:
                await asyncio.to_thread(reader.join, 2)
            quiescent = state.pid is not None and not live_group(
                state.pid, state.start_ticks, state.boot_id
            )
            if state.cgroup_path:
                quiescent = await asyncio.to_thread(self.cgroups.cleanup, state.cgroup_path)
            state.quiescent = quiescent
            if not quiescent:
                state.outcome = Outcome.UNKNOWN
                state.error = "Owned descendants remain"
            elif self.cancelled.get(state.request.attempt_id, -1) >= state.request.generation:
                state.outcome = Outcome.CANCELLED
            elif code != 0 or state.error:
                state.outcome = Outcome.FAILED
                state.error = state.error or f"Tool exited with {code}"
            else:
                result_path = output / "result.json"
                if result_path.stat().st_size > 1048576:
                    raise ValueError("Tool result exceeds output limit")
                result = json.loads(result_path.read_text())
                if result["input_version"] != state.request.input_version:
                    raise ValueError("Result input version mismatch")
                result["artifact_id"] = state.request.attempt_id
                result["artifact_sha256"] = digest(result)
                state.result = result
                state.outcome = Outcome.SUCCEEDED
        except Exception as error:
            state.outcome = Outcome.FAILED if state.quiescent else Outcome.UNKNOWN
            state.error = type(error).__name__ + ": " + str(error)
        finally:
            self._save(state)
            self.processes.pop(state.request.attempt_id, None)
            self.watchers.pop(state.request.attempt_id, None)

    async def get(self, attempt_id: str) -> ToolState | None:
        state = self.states.get(attempt_id)
        return state.model_copy(deep=True) if state else None

    async def cancel(self, attempt_id: str, generation: int) -> bool:
        async with self.lock:
            self.cancelled[attempt_id] = max(generation, self.cancelled.get(attempt_id, -1))
            atomic_json(
                self.directory / "cancelled" / (attempt_id + ".json"),
                {"generation": self.cancelled[attempt_id]},
            )
            state = self.states.get(attempt_id)
            if state is None or state.quiescent:
                return True
        stopped = await asyncio.to_thread(self._stop, state)
        watcher = self.watchers.get(attempt_id)
        if watcher is not None:
            await asyncio.shield(watcher)
        return stopped

    async def close(self) -> None:
        for identity, state in list(self.states.items()):
            if not state.quiescent:
                await self.cancel(identity, state.request.generation + 1)
        await asyncio.gather(*list(self.watchers.values()), return_exceptions=True)
