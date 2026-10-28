import asyncio
import io
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from taskloom.adapters.executor import ProcessExecutor
from taskloom.adapters.linux import CgroupDomain
from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.client.ports import GatewayPort
from taskloom.client.service import ClientService
from taskloom.contracts.models import (
    ClientConfig,
    GatewayRecord,
    ModelReply,
    TaskCreate,
    ToolCall,
    Usage,
    digest,
)
from taskloom.domain.model import DomainError, Outcome
from taskloom.execution.access import local_path, readable


async def wait_until(predicate):
    async with asyncio.timeout(10):
        while not predicate():  # noqa: ASYNC110 - observable service state has no notification API
            await asyncio.sleep(0.01)


async def make_service(tmp_path, feedback, calls=()):
    workspace = tmp_path / "project"
    workspace.mkdir(exist_ok=True)
    profile = tmp_path / "profile"
    config = ClientConfig(workspace_root=str(workspace), memory_budget_mib=512)
    repository = SQLiteRepository(profile / "client.db")

    class ExecutorAdapter(ProcessExecutor):
        async def query(self, attempt_id):
            return await self.get(attempt_id)

    executor = ExecutorAdapter(profile, CgroupDomain(None), workspace_root=str(workspace))
    await executor.start()
    gateway = AsyncMock(spec=GatewayPort)
    gateway.feedback.return_value = feedback
    replies = iter(
        [
            ModelReply(
                tool_calls=list(calls), usage=Usage(input_tokens=1, output_tokens=1, cost_units=2)
            ),
            ModelReply(
                content="已处理。", usage=Usage(input_tokens=1, output_tokens=1, cost_units=2)
            ),
        ]
        if calls
        else [
            ModelReply(content="你好", usage=Usage(input_tokens=1, output_tokens=1, cost_units=2))
            for _ in range(4)
        ]
    )

    async def submit(request):
        return GatewayRecord(
            tenant_id="alice",
            request=request,
            payload_hash=digest(request.model_dump()),
            state="FINISHED",
            outcome=Outcome.SUCCEEDED,
            quiescent=True,
            consumed_units=2,
            reply=next(replies),
        )

    gateway.submit.side_effect = submit
    service = ClientService(repository, gateway, executor, profile, config)
    await service.start()
    return service, repository, gateway, workspace


async def finish(service, repository):
    await service.close()
    await repository.close()


async def test_conversation_history_no_generated_message_files(tmp_path, platform_feedback):
    service, repository, gateway, workspace = await make_service(tmp_path, platform_feedback)
    try:
        first = await service.create(TaskCreate(mode="chat", message="名字是织序", skill="notes"))
        await wait_until(lambda: service.get(first.task_id).outcome != Outcome.PENDING)
        second = await service.create(
            TaskCreate(mode="chat", message="我说的名字？"), first.conversation_id
        )
        await wait_until(lambda: service.get(second.task_id).outcome != Outcome.PENDING)
        assert service.get(second.task_id).outcome == Outcome.SUCCEEDED
        messages = gateway.submit.call_args.args[0].messages
        assert [m["content"] for m in messages[1:]] == ["名字是织序", "你好", "我说的名字？"]
        assert "本轮启用" not in messages[0]["content"]
        assert not list((service.directory / "tasks").rglob("notes.txt"))
        assert not list((service.directory / "tasks").rglob("summary.json"))
    finally:
        await finish(service, repository)


@pytest.mark.parametrize("allowed", [True, False])
async def test_command_confirmation_precedes_reservation_and_can_be_refused(
    tmp_path, platform_feedback, allowed
):
    call = ToolCall(
        call_id="cmd",
        name="run_command",
        arguments={
            "command": "printf confirmed > approval-result.txt; printf done",
            "cwd": ".",
            "timeout_seconds": 5,
        },
    )
    service, repository, gateway, workspace = await make_service(
        tmp_path, platform_feedback, [call]
    )
    try:
        task = await service.create(TaskCreate(mode="chat", message="测试指令"))
        await wait_until(lambda: service.get(task.task_id).pending_approval is not None)
        current = service.get(task.task_id)
        approval = current.pending_approval
        assert service.resources()["allocations"] == []
        assert approval["command"] == call.arguments["command"]
        assert not (workspace / "approval-result.txt").exists()
        with pytest.raises(DomainError, match="已失效"):
            await service.decide_approval(task.task_id, approval["approval_id"], True, 99)
        await service.decide_approval(
            task.task_id, approval["approval_id"], allowed, approval["generation"]
        )
        await wait_until(lambda: service.get(task.task_id).outcome != Outcome.PENDING)
        result = service.get(task.task_id)
        assert result.outcome == Outcome.SUCCEEDED, result.error
        assert (workspace / "approval-result.txt").exists() == allowed
        assert service.resources()["allocations"] == []
        assert gateway.submit.await_count == 2
    finally:
        await finish(service, repository)


async def test_external_read_requires_exact_path_confirmation(tmp_path, platform_feedback):
    target = tmp_path / "outside.txt"
    target.write_text("外部资料", encoding="utf-8")
    call = ToolCall(
        call_id="read", name="read_file", arguments={"path": str(target), "max_bytes": 1024}
    )
    service, repository, gateway, workspace = await make_service(
        tmp_path, platform_feedback, [call]
    )
    try:
        task = await service.create(TaskCreate(mode="chat", message="读取文件"))
        await wait_until(lambda: service.get(task.task_id).pending_approval is not None)
        approval = service.get(task.task_id).pending_approval
        assert approval["path"] == str(target)
        await service.decide_approval(task.task_id, approval["approval_id"], True, 0)
        await wait_until(lambda: service.get(task.task_id).outcome != Outcome.PENDING)
        assert service.get(task.task_id).outcome == Outcome.SUCCEEDED, service.get(
            task.task_id
        ).error
        tool = next(a for a in service.attempts.values() if a.kind == "tool")
        assert tool.result["content"] == "外部资料"
        assert tool.request_payload["approved_external_path"] == str(target)
    finally:
        await finish(service, repository)


async def test_missing_file_is_observation_and_agent_can_respond(tmp_path, platform_feedback):
    call = ToolCall(
        call_id="read", name="read_file", arguments={"path": "missing.txt", "max_bytes": 1024}
    )
    service, repository, gateway, workspace = await make_service(
        tmp_path, platform_feedback, [call]
    )
    try:
        task = await service.create(TaskCreate(mode="chat", message="读取文件"))
        await wait_until(lambda: service.get(task.task_id).outcome != Outcome.PENDING)
        assert service.get(task.task_id).outcome == Outcome.SUCCEEDED, service.get(
            task.task_id
        ).error
        tools = [a for a in service.attempts.values() if a.kind == "tool"]
        assert tools[0].outcome == Outcome.FAILED
        assert "File not found" in tools[0].result["error"]
        assert service.resources()["allocations"] == []
    finally:
        await finish(service, repository)


async def test_cancel_during_confirmation_invalidates_approval(tmp_path, platform_feedback):
    call = ToolCall(
        call_id="cmd",
        name="run_command",
        arguments={"command": "touch should-not-exist", "cwd": ".", "timeout_seconds": 5},
    )
    service, repository, gateway, workspace = await make_service(
        tmp_path, platform_feedback, [call]
    )
    try:
        task = await service.create(TaskCreate(mode="chat", message="执行指令"))
        await wait_until(lambda: service.get(task.task_id).pending_approval is not None)
        approval = service.get(task.task_id).pending_approval
        await service.cancel(task.task_id)
        await wait_until(lambda: task.task_id not in service.runners)
        with pytest.raises(DomainError):
            await service.decide_approval(task.task_id, approval["approval_id"], True, 0)
        assert not (workspace / "should-not-exist").exists()
        assert service.resources()["allocations"] == []
        assert service.get(task.task_id).outcome == Outcome.CANCELLED
    finally:
        await finish(service, repository)


def test_path_policy_resolves_symlinks_and_private_paths(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (root / "alias").symlink_to(outside)
    resolved = local_path("alias", root)
    assert not readable(resolved, root, tmp_path / "tasks")
    assert readable(resolved, root, tmp_path / "tasks", str(outside))
    assert not readable(root / ".env", root, tmp_path / "tasks")
    assert str(local_path(r"E:\Project\file.txt", root)) == "/mnt/e/Project/file.txt"


async def test_local_image_returns_vision_and_preserves_source_format(tmp_path, platform_feedback):
    service, repository, gateway, workspace = await make_service(
        tmp_path,
        platform_feedback,
        [
            ToolCall(
                call_id="image", name="read_file", arguments={"path": "red.png", "max_bytes": 1024}
            )
        ],
    )
    output = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(output, format="PNG")
    (workspace / "red.png").write_bytes(output.getvalue())
    try:
        task = await service.create(TaskCreate(mode="chat", message="读取本地图片"))
        await wait_until(lambda: service.get(task.task_id).outcome != Outcome.PENDING)
        assert service.get(task.task_id).outcome == Outcome.SUCCEEDED
        tool = next(a for a in service.attempts.values() if a.kind == "tool")
        assert tool.result["mime_type"] == "image/png"
        assert tool.result["image_mime_type"] == "image/jpeg"
        messages = gateway.submit.call_args.args[0].messages
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;")
    finally:
        await finish(service, repository)


@pytest.mark.parametrize("tool", ["read_file", "run_command"])
async def test_worker_bounds_utf8_or_command_output(tmp_path, platform_feedback, monkeypatch, tool):
    monkeypatch.setenv("TASKLOOM_TEST_SECRET", "should-not-leak")
    args = (
        {"path": "中文.txt", "max_bytes": 4}
        if tool == "read_file"
        else {
            "command": "python3 -c \"import os; print(os.getenv('TASKLOOM_TEST_SECRET','clean')); print('x'*100000)\"",
            "cwd": ".",
            "timeout_seconds": 5,
        }
    )
    service, repository, gateway, workspace = await make_service(
        tmp_path, platform_feedback, [ToolCall(call_id="bounded", name=tool, arguments=args)]
    )
    (workspace / "中文.txt").write_text("中文文本", encoding="utf-8")
    try:
        task = await service.create(TaskCreate(mode="chat", message="测试有界输出"))
        if tool == "run_command":
            await wait_until(lambda: service.get(task.task_id).pending_approval is not None)
            approval = service.get(task.task_id).pending_approval
            await service.decide_approval(task.task_id, approval["approval_id"], True, 0)
        await wait_until(lambda: service.get(task.task_id).outcome != Outcome.PENDING)
        assert service.get(task.task_id).outcome == Outcome.SUCCEEDED
        result = next(a.result for a in service.attempts.values() if a.kind == "tool")
        if tool == "read_file":
            assert result["content"] == "中" and result["truncated"]
        else:
            assert result["output"].startswith("clean\n")
            assert len(result["output"].encode()) == 65536 and result["output_truncated"]
    finally:
        await finish(service, repository)
