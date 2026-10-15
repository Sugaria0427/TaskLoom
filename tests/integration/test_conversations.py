from unittest.mock import AsyncMock

import pytest

from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.client.ports import ExecutorPort, GatewayPort
from taskloom.client.service import ClientService
from taskloom.contracts.models import (
    ClientConfig,
    ConversationUpdate,
    TaskCreate,
    TaskState,
    digest,
)
from taskloom.domain.model import DomainError, Outcome, Resources, WorkState
from taskloom.domain.ports import StoredRecord


async def test_conversations_preserve_ownership_roots_and_archive_semantics(
    tmp_path, platform_feedback
) -> None:
    repository = SQLiteRepository(tmp_path / "client.db")
    gateway = AsyncMock(spec=GatewayPort)
    gateway.feedback.return_value = platform_feedback
    executor = AsyncMock(spec=ExecutorPort)
    service = ClientService(repository, gateway, executor, tmp_path, ClientConfig(max_inflight=1))
    await service.start()
    service.models.pool.reserve("hold", Resources(slots=1))
    try:
        conversation = await service.create_conversation("研究笔记")
        first = await service.create(
            TaskCreate(instruction="统计第一份资料", documents={"a.txt": "one two"}),
            conversation.conversation_id,
        )
        second = await service.create(
            TaskCreate(documents={"b.txt": "three"}), conversation.conversation_id
        )
        assert first.conversation_id == second.conversation_id == conversation.conversation_id
        assert first.task_id != second.task_id
        assert first.agent_id != second.agent_id
        summary = service.conversation_summaries()[0]
        assert summary["task_count"] == 2 and summary["active_count"] == 2
        assert summary["tenant_id"] == "alice"
        await service.update_conversation(
            conversation.conversation_id, ConversationUpdate(title="统计实验", archived=True)
        )
        with pytest.raises(DomainError, match="Restore"):
            await service.create(TaskCreate(), conversation.conversation_id)
        assert len(service.tasks) == 2
        assert service.get(first.task_id).outcome == Outcome.PENDING
        await service.update_conversation(
            conversation.conversation_id, ConversationUpdate(archived=False)
        )
        record = await repository.get("conversation", conversation.conversation_id)
        assert record["title"] == "统计实验" and not record["archived"]
        saved = await repository.get("task", first.task_id)
        assert saved["specification"]["instruction"] == "统计第一份资料"
        with pytest.raises(DomainError, match="not found"):
            service.conversation("another-account-conversation")
        assert service.resources()["model_capacity"]["slots"] == 1
    finally:
        await service.close()
        await repository.close()


async def test_legacy_tasks_gain_durable_conversations_once(tmp_path, platform_feedback) -> None:
    path = tmp_path / "client.db"
    specification = TaskCreate(title="旧任务", documents={"old.txt": "one two"})
    legacy = TaskState(
        task_id="legacy-task",
        agent_id="legacy-agent",
        tenant_id="alice",
        specification=specification,
        input_version=digest(specification.documents),
        deadline_at="2026-01-01T00:00:00+00:00",
        state=WorkState.FINISHED,
        outcome=Outcome.SUCCEEDED,
        result={"files": 1, "words": 2, "summary": "原有成果"},
    ).model_dump(mode="json")
    legacy.pop("conversation_id")
    legacy["specification"].pop("instruction")
    repository = SQLiteRepository(path)
    await repository.apply([StoredRecord("task", "legacy-task", "alice", legacy)])
    await repository.close()
    for _ in range(2):
        repository = SQLiteRepository(path)
        gateway = AsyncMock(spec=GatewayPort)
        gateway.feedback.return_value = platform_feedback
        service = ClientService(
            repository, gateway, AsyncMock(spec=ExecutorPort), tmp_path, ClientConfig()
        )
        await service.start()
        try:
            assert len(service.conversations) == 1
            assert service.get("legacy-task").conversation_id == "legacy-task"
            assert service.get("legacy-task").result["summary"] == "原有成果"
            assert service.conversation("legacy-task").title == "旧任务"
            gateway.submit.assert_not_awaited()
        finally:
            await service.close()
            await repository.close()
