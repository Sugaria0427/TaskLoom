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
from taskloom.execution.coordination import (
    AccessCoordinator,
    PublishedOutcome,
    ResourceKey,
    SharedPreparationFailed,
    SharingUnavailable,
)

# How many times a waiter re-applies after the owner it waited for failed.
REJOIN_LIMIT = 3


class ProcessExecutor:
    """Persistent launch intent + one owned process group per Attempt."""

    def __init__(
        self,
        profile_dir: Path,
        cgroups: CgroupDomain,
        slots: int = 2,
        workspace_root: str | None = None,
        access: AccessCoordinator | None = None,
        max_waiters: int = 64,
    ) -> None:
        """Open the executor state of one client profile.

        Args:
            profile_dir: Client profile directory shared with the client.
            cgroups: Cgroup domain used to bound and measure tool processes.
            slots: How many tool processes may run at the same time.
            workspace_root: Root that read_file and run_command may serve;
                defaults to a directory under the profile.
            access: Shared-access coordinator; one is created when omitted.
            max_waiters: How many attempts may wait for another attempt's
                preparation at once; waiters hold no slot, and an attempt
                beyond this bound runs unshared in the slot it was admitted to.
        """
        self.directory = profile_dir
        self.cgroups = cgroups
        self.slots = slots
        self.max_waiters = max_waiters
        self.access = access if access is not None else AccessCoordinator(profile_dir / "access")
        self.states: dict[str, ToolState] = {}
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.watchers: dict[str, asyncio.Task[None]] = {}
        # Attempts between admission and a settled launch decision: cancel
        # waits for these instead of reporting a process that may still start.
        self.starting: dict[str, asyncio.Future[None]] = {}
        self.cancelled: dict[str, int] = {}
        self.permits: dict[str, str] = {}
        self.lock = asyncio.Lock()
        self.workspace_root = (
            Path(workspace_root).resolve()
            if workspace_root
            else (profile_dir / "workspace").resolve()
        )
        self.workspace_root.mkdir(parents=True, exist_ok=True)
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
                if state.share_role == "waiter" and state.pid is None:
                    # A waiter never starts a process of its own, so nothing
                    # of it can still run; it fails cleanly and may be retried.
                    state.quiescent = True
                    state.outcome = Outcome.FAILED
                    state.error = "EXECUTOR_RESTARTED: shared wait ended before any launch"
                else:
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

    def _key(self, request: ToolRun) -> ResourceKey:
        return ResourceKey.of(request, workspace_root=str(self.workspace_root))

    def _is_cancelled(self, request: ToolRun) -> bool:
        return self.cancelled.get(request.attempt_id, -1) >= request.generation

    def _running(self) -> int:
        """Count attempts that hold a slot; waiters run no process and hold none."""
        return sum(
            not item.quiescent and item.share_role != "waiter" for item in self.states.values()
        )

    def _waiting(self) -> int:
        return sum(
            not item.quiescent and item.share_role == "waiter" for item in self.states.values()
        )

    def _require_slot(self) -> None:
        """Reject a launch of an admitted attempt that would exceed the slots.

        The attempt itself is already counted, so only a surplus is rejected;
        that happens when it was admitted as a likely waiter.

        Raises:
            DomainError: ``EXECUTOR_FULL`` (retryable).
        """
        if self._running() > self.slots:
            raise DomainError("EXECUTOR_FULL", "Executor capacity exhausted", True)

    def _settle_cancelled(self, state: ToolState) -> ToolState:
        """Settle an attempt that was cancelled before any process started."""
        self.permits.pop(state.request.attempt_id, None)
        state.outcome = Outcome.CANCELLED
        state.quiescent = True
        self._save(state)
        return state.model_copy(deep=True)

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
        key = self._key(request)
        async with self.lock:
            previous = self.states.get(request.attempt_id)
            if previous is not None:
                if previous.request != request:
                    raise DomainError("ATTEMPT_CONFLICT", "Tool attempt changed")
                return previous.model_copy(deep=True)
            state = ToolState(request=request)
            if request.workspace_root is not None and request.workspace_root != str(
                self.workspace_root
            ):
                raise DomainError(
                    "WORKSPACE_CONFLICT", "Tool workspace differs from executor configuration"
                )
            if request.tool_name == "run_command" and not request.command_approved:
                raise DomainError("APPROVAL_REQUIRED", "Command was not approved")
            if self._is_cancelled(request):
                state.outcome = Outcome.CANCELLED
                state.quiescent = True
                self.states[request.attempt_id] = state
                self._save(state)
                return state
            # An attempt that will most likely wait for an in-flight preparation
            # needs no slot; _admit re-checks before it launches anything.
            joins = key.shareable and self.access.in_flight(key)
            if not joins and self._running() >= self.slots:
                raise DomainError("EXECUTOR_FULL", "Executor capacity exhausted", True)
            self.states[request.attempt_id] = state
            starting = asyncio.get_running_loop().create_future()
            self.starting[request.attempt_id] = starting
        try:
            return await self._admit(state, request, key)
        finally:
            if self.starting.get(request.attempt_id) is starting:
                self.starting.pop(request.attempt_id, None)
            starting.set_result(None)

    async def _admit(self, state: ToolState, request: ToolRun, key: ResourceKey) -> ToolState:
        """Decide whether an admitted attempt reuses, waits, or launches."""
        claimed = False
        try:
            workspace = self._staged_workspace(request)
            output_dir = workspace / "attempts" / request.attempt_id
            output_dir.mkdir(parents=True, exist_ok=True)
            if not key.shareable:
                # read_file, run_command and anything approved touch more than
                # the staged inputs: they always run themselves.
                return await self._launch(state, request, workspace, output_dir, key, shared=False)
            published = await self.access.lookup(key)
            if self._is_cancelled(request):
                return self._settle_cancelled(state)
            if published is not None:
                state.share_role = "shared"
                self._reuse(state, request, output_dir, published, role="shared", waited_ms=0.0)
                return state.model_copy(deep=True)
            if not self.access.claim(key, request.attempt_id):
                if self._waiting() >= self.max_waiters:
                    self._require_slot()
                    return await self._launch(
                        state, request, workspace, output_dir, key, shared=False
                    )
                # Persist the waiter before reporting it, so a restart can tell
                # that this attempt never started a process.
                state.share_role = "waiter"
                self._save(state)
                self.watchers[request.attempt_id] = asyncio.create_task(
                    self._wait_for_shared(state, request, workspace, output_dir, key)
                )
                return state.model_copy(deep=True)
            claimed = True
            self._require_slot()
            state.share_role = "owner"
            return await self._launch(state, request, workspace, output_dir, key, shared=True)
        except BaseException as error:
            self._forget_unlaunched(request.attempt_id)
            if claimed:
                # Release the claim so the next attempt for this key can own it
                # again. _launch reports its own Popen failure and cancellation
                # without raising; fail() is idempotent, so no path fails the
                # key twice with a stale future left behind.
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
        key = self._key(request)
        result = json.loads(json.dumps(published.payload))
        result["artifact_id"] = request.attempt_id
        result["access"] = self._evidence(
            key, published, role=role, waited_ms=waited_ms, permit_id=None
        )
        result["artifact_sha256"] = digest(result)
        (output_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
        artifact_bytes = sum(
            path.stat().st_size for path in output_dir.rglob("*") if path.is_file()
        )
        state.result = result
        state.artifact_bytes = artifact_bytes
        state.outcome = Outcome.SUCCEEDED
        state.quiescent = True
        self._save(state)

    def _fail_waiter(self, state: ToolState, error: str) -> None:
        state.error = error
        state.outcome = Outcome.FAILED
        state.quiescent = True
        self._save(state)

    async def _wait_for_shared(
        self,
        state: ToolState,
        request: ToolRun,
        workspace: Path,
        output_dir: Path,
        key: ResourceKey,
    ) -> None:
        """Wait for another attempt's preparation and reuse it.

        When the owner fails or is cancelled the waiter applies again: if it
        wins the claim it launches the preparation itself, otherwise it waits
        for the new owner. When the coordinator cannot track one more shared
        wait, the waiter runs the tool unshared.
        """
        started = time.monotonic()
        current = asyncio.current_task()
        failure = "Shared preparation failed"
        try:
            for _ in range(REJOIN_LIMIT):
                try:
                    published = await self.access.await_outcome(key)
                except SharingUnavailable:
                    await self._promote(state, request, workspace, output_dir, key, shared=False)
                    return
                except SharedPreparationFailed as error:
                    failure = str(error)
                    if self._is_cancelled(request):
                        self._settle_cancelled(state)
                        return
                    if self.access.claim(key, request.attempt_id):
                        await self._promote(state, request, workspace, output_dir, key, shared=True)
                        return
                    continue
                waited_ms = round((time.monotonic() - started) * 1000, 3)
                self._staged_workspace(request)
                self._reuse(
                    state, request, output_dir, published, role="waiter", waited_ms=waited_ms
                )
                return
            self._fail_waiter(state, "SHARED_PREPARATION_FAILED: " + failure)
        except asyncio.CancelledError:
            if state.pid is None and not state.quiescent:
                state.outcome = Outcome.CANCELLED
                state.quiescent = True
                self._save(state)
            raise
        except DomainError as error:
            if state.pid is None and not state.quiescent:
                self._fail_waiter(state, error.code + ": " + str(error))
        except Exception as error:
            if state.pid is None and not state.quiescent:
                self._fail_waiter(state, "SHARED_WAIT_FAILED: " + type(error).__name__)
        finally:
            if self.watchers.get(request.attempt_id) is current:
                self.watchers.pop(request.attempt_id, None)

    async def _promote(
        self,
        state: ToolState,
        request: ToolRun,
        workspace: Path,
        output_dir: Path,
        key: ResourceKey,
        *,
        shared: bool,
    ) -> None:
        """Launch the tool for a former waiter, as the new owner or unshared.

        Raises:
            DomainError: No slot is free for the launch, or the staged
                workspace changed while the attempt waited.
        """
        starting = asyncio.get_running_loop().create_future()
        self.starting[request.attempt_id] = starting
        try:
            if self._is_cancelled(request):
                if shared:
                    await self.access.fail(key, "Attempt cancelled before launch")
                self._settle_cancelled(state)
                return
            if self._running() >= self.slots:
                if shared:
                    await self.access.fail(key, "Executor capacity exhausted")
                raise DomainError("EXECUTOR_FULL", "Executor capacity exhausted", True)
            state.share_role = "owner" if shared else None
            try:
                self._staged_workspace(request)
                await self._launch(state, request, workspace, output_dir, key, shared=shared)
            except BaseException as error:
                if shared:
                    await self.access.fail(key, f"{type(error).__name__}: {error}")
                raise
        finally:
            if self.starting.get(request.attempt_id) is starting:
                self.starting.pop(request.attempt_id, None)
            starting.set_result(None)

    async def _launch(
        self,
        state: ToolState,
        request: ToolRun,
        workspace: Path,
        output_dir: Path,
        key: ResourceKey,
        *,
        shared: bool,
    ) -> ToolState:
        """Start the owned tool process of one attempt.

        The saved cancellation intent is checked right before and right after
        the process starts, so a cancel that arrives while the process is being
        created stops it instead of reporting a process that never existed.
        """
        group = self.cgroups.create(
            request.attempt_id, request.memory_bytes, request.cpu_quota, request.cpu_period
        )
        state.cgroup_path = str(group) if group else None
        permit = self.access.issue_permit(
            key,
            holder=request.attempt_id,
            owner=request.attempt_id,
            mode="write" if shared else "read",
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
            "workspace_root": str(self.workspace_root),
            "tasks_root": str((self.directory / "tasks").resolve()),
            "approved_external_path": request.approved_external_path,
            "command_approved": request.command_approved,
            "approved_command_cwd": request.approved_command_cwd,
            "attempt_id": request.attempt_id,
            "access": {
                "permit": {**permit.body(), "signature": permit.signature},
                "secret_path": str(self.access.secret_path),
                "locks": [
                    {"path": str(path), "mode": mode}
                    for path, mode in (self.access.tool_locks(key) if shared else [])
                ],
            },
        }
        atomic_json(output_dir / "spec.json", spec)
        self._save(state)
        if self._is_cancelled(request):
            if state.cgroup_path:
                self.cgroups.cleanup(state.cgroup_path)
            if shared:
                await self.access.fail(key, "Attempt cancelled before launch")
            return self._settle_cancelled(state)
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
            self.permits.pop(request.attempt_id, None)
            state.error = str(error)
            state.outcome = Outcome.FAILED
            state.quiescent = True
            self._save(state)
            if shared:
                await self.access.fail(key, str(error))
            return state.model_copy(deep=True)
        state.pid = process.pid
        state.start_ticks = process_ticks(process.pid)
        state.boot_id = boot_id()
        self.processes[request.attempt_id] = process
        self._save(state)
        if self._is_cancelled(request):
            # Cancelled while the process was being created: stop it now; the
            # watcher below settles it as CANCELLED once it has exited.
            await asyncio.to_thread(self._stop, state)
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
            self._watch(state, process, readers, output_dir, key, shared)
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
        key: ResourceKey,
        shared: bool,
    ) -> None:
        """Settle one launched attempt once its process group is gone.

        Every terminal field is computed into locals first (including the
        stderr diagnostic and the shared publish, which await). The state is
        then written without any await in between: error and result, then
        outcome, then quiescent last. A poller therefore never sees a final
        outcome with quiescent still False, nor quiescent with a PENDING
        outcome.
        """
        quiescent = False
        try:
            error: str | None = None
            result: JsonObject | None = None
            try:
                code = await asyncio.wait_for(
                    asyncio.to_thread(process.wait), state.request.timeout_seconds
                )
            except TimeoutError:
                await asyncio.to_thread(self._stop, state)
                code = await asyncio.to_thread(process.wait)
                error = "TOOL_TIMEOUT"
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
                outcome = Outcome.UNKNOWN
                error = "Owned descendants remain"
            elif self._is_cancelled(state.request):
                outcome = Outcome.CANCELLED
            elif state.oom:
                outcome = Outcome.FAILED
                error = "TOOL_OOM"
            elif code != 0 or error:
                if not error and state.request.tool_name in ("read_file", "run_command"):
                    diagnostic = await asyncio.to_thread(
                        lambda: (output / "stderr.log").read_text(errors="replace")[-2000:]
                    )
                    error = f"Tool exited with {code}: " + diagnostic
                error = error or f"Tool exited with {code}"
                outcome = Outcome.FAILED
            else:
                result_path = output / "result.json"
                if result_path.stat().st_size > 1048576:
                    raise ValueError("Tool result exceeds output limit")
                raw = json.loads(result_path.read_text())
                if raw["input_version"] != state.request.input_version:
                    raise ValueError("Result input version mismatch")
                result = json.loads(json.dumps(raw))
                result["artifact_id"] = state.request.attempt_id
                if shared:
                    published = await self.access.publish(key, raw, state.request.attempt_id)
                    result["access"] = self._evidence(
                        key,
                        published,
                        role="owner",
                        waited_ms=0.0,
                        permit_id=self.permits.get(state.request.attempt_id),
                    )
                result["artifact_sha256"] = digest(result)
                outcome = Outcome.SUCCEEDED
            # No await from here on: the settled fields appear together.
            state.error = error
            state.result = result
            state.outcome = outcome
            state.quiescent = quiescent
        except Exception as failure:
            state.error = type(failure).__name__ + ": " + str(failure)
            state.outcome = Outcome.FAILED if quiescent else Outcome.UNKNOWN
            state.quiescent = quiescent
        finally:
            if shared and state.outcome != Outcome.SUCCEEDED:
                await self.access.fail(key, state.error or "Tool execution failed")
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

        The cancellation intent is saved first. An attempt that is still being
        admitted or launched is waited for: the launch re-checks the saved
        intent and stops a process it has just created, so ``True`` is never
        returned for a process that starts afterwards.

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
        starting = self.starting.get(attempt_id)
        if starting is not None:
            await asyncio.shield(starting)
            state = self.states.get(attempt_id)
            if state is None or state.quiescent:
                return True
        if state.pid is None:
            if attempt_id in self.starting:
                # A waiter began its own launch meanwhile; wait for it again.
                return await self.cancel(attempt_id, generation)
            # A waiter: it never started a process of its own.
            watcher = self.watchers.get(attempt_id)
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            if not state.quiescent:
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
