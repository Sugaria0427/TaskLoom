import asyncio
import json
import subprocess
import sys
import time
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
from taskloom.contracts.models import JsonObject, ToolRun, ToolState, digest
from taskloom.domain.model import DomainError, Outcome
from taskloom.execution.access import AccessCoordinator, PublishedOutcome, ResourceKey


class ProcessExecutor:
    """Persistent launch intent + one owned process group per Attempt."""

    def __init__(
        self,
        profile_dir: Path,
        cgroups: CgroupDomain,
        slots: int = 2,
        access: AccessCoordinator | None = None,
    ) -> None:
        """Open the executor state of one client profile.

        Args:
            profile_dir: Client profile directory shared with the client.
            cgroups: Cgroup domain used to bound and measure tool processes.
            slots: How many tool processes may run at the same time.
            access: Shared-access coordinator; one is created when omitted.
        """
        self.directory = profile_dir
        self.cgroups = cgroups
        self.slots = slots
        self.access = access if access is not None else AccessCoordinator(profile_dir / "access")
        self.states: dict[str, ToolState] = {}
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.watchers: dict[str, asyncio.Task[None]] = {}
        self.cancelled: dict[str, int] = {}
        self.permits: dict[str, str] = {}
        self.lock = asyncio.Lock()
        (self.directory / "executions").mkdir(parents=True, exist_ok=True)
        (self.directory / "cancelled").mkdir(exist_ok=True)

    def _save(self, state: ToolState) -> None:
        atomic_json(
            self.directory / "executions" / (state.request.attempt_id + ".json"),
            state.model_dump(mode="json"),
        )

    async def start(self) -> None:
        for path in (self.directory / "executions").glob("*.tmp"):
            path.unlink(missing_ok=True)
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
        """Admit one tool attempt, then launch or share its execution.

        Args:
            request: Tool request of one attempt.

        Returns:
            The attempt state. A reused preparation comes back finished; an
            attempt that waits for another attempt's preparation comes back
            pending and is settled by its watcher.

        Raises:
            DomainError: The attempt conflicts, exceeds the slot count, or its
                staged workspace no longer matches the requested input version.
        """
        key = ResourceKey.of(request)
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
            self.states[request.attempt_id] = state
        claimed = False
        try:
            workspace = self._staged_workspace(request)
            output_dir = workspace / "attempts" / request.attempt_id
            output_dir.mkdir(parents=True, exist_ok=True)
            published = await self.access.lookup(key)
            if published is not None:
                self._reuse(state, request, output_dir, published, role="shared", waited_ms=0.0)
                return state.model_copy(deep=True)
            if not self.access.claim(key, request.attempt_id):
                self.watchers[request.attempt_id] = asyncio.create_task(
                    self._wait_for_shared(state, request, output_dir, key)
                )
                return state.model_copy(deep=True)
            claimed = True
            return await self._launch(state, request, workspace, output_dir, key)
        except BaseException as error:
            self._forget_unlaunched(request.attempt_id)
            if claimed:
                # Release the claim so the next attempt for this key can own it
                # again. _launch's Popen branch reports its own failure without
                # raising; fail() is idempotent, so no path fails the key twice
                # with a stale future left behind.
                await self.access.fail(key, f"{type(error).__name__}: {error}")
            raise

    def _forget_unlaunched(self, attempt_id: str) -> None:
        pending = self.states.get(attempt_id)
        if pending is not None and pending.pid is None and not pending.quiescent:
            self.states.pop(attempt_id, None)

    def _staged_workspace(self, request: ToolRun) -> Path:
        """Validate the staged workspace of one request and return it.

        Args:
            request: Tool request naming the root task and input version.

        Returns:
            The workspace directory that holds the staged inputs.

        Raises:
            DomainError: The manifest is missing, the workspace belongs to
                another account, or the staged bytes changed.
        """
        workspace = self.directory / "tasks" / request.root_task_id
        manifest = workspace / "input.json"
        if (
            not manifest.exists()
            or json.loads(manifest.read_text())["input_version"] != request.input_version
        ):
            raise DomainError("INPUT_VERSION_MISMATCH", "Staged input version differs")
        owner = json.loads(manifest.read_text()).get("tenant_id")
        if owner is not None and owner != request.tenant_id:
            raise DomainError("UNAUTHORIZED", "Tool workspace belongs to another platform account")
        input_paths = list((workspace / "inputs").iterdir())
        if any(path.is_symlink() or not path.is_file() for path in input_paths):
            raise DomainError("INPUT_VERSION_MISMATCH", "Unexpected staged input object")
        actual = {path.name: path.read_bytes().decode("utf-8") for path in input_paths}
        if digest(actual) != request.input_version:
            raise DomainError("INPUT_VERSION_MISMATCH", "Staged file contents changed")
        return workspace

    def _evidence(
        self,
        key: ResourceKey,
        published: PublishedOutcome,
        *,
        role: str,
        waited_ms: float,
        permit_id: str | None,
    ) -> JsonObject:
        """Describe how one attempt took part in a shared preparation."""
        return {
            "resource": key.slug,
            "role": role,
            "owner": published.owner,
            "version": published.version,
            "users": max(published.users, self.access.users(key)),
            "waited_ms": waited_ms,
            "permit_id": permit_id,
        }

    def _reuse(
        self,
        state: ToolState,
        request: ToolRun,
        output_dir: Path,
        published: PublishedOutcome,
        *,
        role: str,
        waited_ms: float,
    ) -> None:
        """Turn a published preparation into this attempt's own artifact.

        Raises:
            DomainError: The shared preparation was made for another input
                version, so it may not stand in for this attempt.
        """
        if published.payload.get("input_version") != request.input_version:
            raise DomainError("INPUT_VERSION_MISMATCH", "Shared preparation version differs")
        key = ResourceKey.of(request)
        result = json.loads(json.dumps(published.payload))
        result["artifact_id"] = request.attempt_id
        result["access"] = self._evidence(
            key, published, role=role, waited_ms=waited_ms, permit_id=None
        )
        result["artifact_sha256"] = digest(result)
        (output_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
        state.result = result
        state.outcome = Outcome.SUCCEEDED
        state.quiescent = True
        state.artifact_bytes = sum(
            path.stat().st_size for path in output_dir.rglob("*") if path.is_file()
        )
        self._save(state)
        self.watchers.pop(request.attempt_id, None)

    async def _wait_for_shared(
        self,
        state: ToolState,
        request: ToolRun,
        output_dir: Path,
        key: ResourceKey,
    ) -> None:
        """Wait for another attempt's preparation and reuse it."""
        started = time.monotonic()
        try:
            published = await self.access.await_outcome(key)
        except asyncio.CancelledError:
            state.outcome = Outcome.CANCELLED
            state.quiescent = True
            self._save(state)
            raise
        except Exception as error:
            state.outcome = Outcome.FAILED
            state.quiescent = True
            state.error = "Shared preparation failed: " + str(error)
            self._save(state)
            self.watchers.pop(request.attempt_id, None)
            return
        waited_ms = round((time.monotonic() - started) * 1000, 3)
        try:
            self._staged_workspace(request)
            self._reuse(state, request, output_dir, published, role="waiter", waited_ms=waited_ms)
        except DomainError as error:
            state.outcome = Outcome.FAILED
            state.quiescent = True
            state.error = error.code + ": " + str(error)
            self._save(state)
            self.watchers.pop(request.attempt_id, None)

    async def _launch(
        self,
        state: ToolState,
        request: ToolRun,
        workspace: Path,
        output_dir: Path,
        key: ResourceKey,
    ) -> ToolState:
        """Start the owned tool process of the attempt that prepares the resource."""
        group = self.cgroups.create(
            request.attempt_id, request.memory_bytes, request.cpu_quota, request.cpu_period
        )
        state.cgroup_path = str(group) if group else None
        permit = self.access.issue_permit(
            key,
            holder=request.attempt_id,
            owner=request.attempt_id,
            mode="write",
            input_version=request.input_version,
        )
        self.permits[request.attempt_id] = permit.permit_id
        spec: JsonObject = {
            "workspace": str(workspace),
            "output": str(output_dir / "result.json"),
            "input_version": request.input_version,
            "work_iterations": request.work_iterations,
            "emit_bytes": request.emit_bytes,
            "cgroup_path": state.cgroup_path,
            "tool_name": request.tool_name,
            "arguments": request.arguments,
            "attempt_id": request.attempt_id,
            "access": {
                "permit": {**permit.body(), "signature": permit.signature},
                "secret_path": str(self.access.secret_path),
                "locks": [
                    {"path": str(path), "mode": mode}
                    for path, mode in self.access.tool_locks(key, request.root_task_id)
                ],
            },
        }
        atomic_json(output_dir / "spec.json", spec)
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
            await self.access.fail(key, str(error))
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
        quiescent = False
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
            metrics: dict[str, int] = {}
            if state.cgroup_path:
                metrics = await asyncio.to_thread(self.cgroups.metrics, state.cgroup_path)
            state.peak_memory_bytes = metrics.get("peak_memory")
            state.cpu_seconds = metrics["cpu_usec"] / 1_000_000 if "cpu_usec" in metrics else None
            state.oom = metrics.get("oom_kill", 0) > 0
            quiescent = state.pid is not None and not live_group(
                state.pid, state.start_ticks, state.boot_id
            )
            if state.cgroup_path:
                quiescent = await asyncio.to_thread(self.cgroups.cleanup, state.cgroup_path)
            if not quiescent:
                state.outcome = Outcome.UNKNOWN
                state.error = "Owned descendants remain"
            elif self.cancelled.get(state.request.attempt_id, -1) >= state.request.generation:
                state.outcome = Outcome.CANCELLED
            elif state.oom:
                state.outcome = Outcome.FAILED
                state.error = "TOOL_OOM"
            elif code != 0 or state.error:
                state.outcome = Outcome.FAILED
                state.error = state.error or f"Tool exited with {code}"
            else:
                result_path = output / "result.json"
                if result_path.stat().st_size > 1048576:
                    raise ValueError("Tool result exceeds output limit")
                raw = json.loads(result_path.read_text())
                if raw["input_version"] != state.request.input_version:
                    raise ValueError("Result input version mismatch")
                key = ResourceKey.of(state.request)
                published = await self.access.publish(key, raw, state.request.attempt_id)
                result = json.loads(json.dumps(raw))
                result["artifact_id"] = state.request.attempt_id
                result["access"] = self._evidence(
                    key,
                    published,
                    role="owner",
                    waited_ms=0.0,
                    permit_id=self.permits.get(state.request.attempt_id),
                )
                result["artifact_sha256"] = digest(result)
                state.result = result
                state.outcome = Outcome.SUCCEEDED
            # Publish the settled state in one step: a poller (the client
            # service) must never observe quiescent=True while the outcome is
            # still PENDING, even though publishing above really awaits.
            state.quiescent = quiescent
        except Exception as error:
            state.outcome = Outcome.FAILED if quiescent else Outcome.UNKNOWN
            state.error = type(error).__name__ + ": " + str(error)
            state.quiescent = quiescent
        finally:
            if state.outcome != Outcome.SUCCEEDED:
                await self.access.fail(
                    ResourceKey.of(state.request), state.error or "Tool execution failed"
                )
            state.artifact_bytes = await asyncio.to_thread(
                lambda: sum(path.stat().st_size for path in output.rglob("*") if path.is_file())
            )
            self._save(state)
            self.processes.pop(state.request.attempt_id, None)
            self.permits.pop(state.request.attempt_id, None)
            self.watchers.pop(state.request.attempt_id, None)

    async def get(self, attempt_id: str) -> ToolState | None:
        state = self.states.get(attempt_id)
        return state.model_copy(deep=True) if state else None

    async def cancel(self, attempt_id: str, generation: int) -> bool:
        """Cancel one attempt and report whether its resources are released.

        Args:
            attempt_id: Attempt to stop.
            generation: Cancellation generation requested by the client.

        Returns:
            ``True`` when nothing of that attempt is still running.
        """
        async with self.lock:
            self.cancelled[attempt_id] = max(generation, self.cancelled.get(attempt_id, -1))
            atomic_json(
                self.directory / "cancelled" / (attempt_id + ".json"),
                {"generation": self.cancelled[attempt_id]},
            )
            state = self.states.get(attempt_id)
            if state is None or state.quiescent:
                return True
        if state.pid is None:
            watcher = self.watchers.get(attempt_id)
            if watcher is not None:
                watcher.cancel()
            state.outcome = Outcome.CANCELLED
            state.quiescent = True
            self._save(state)
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
