import base64
import io

import pytest
from PIL import Image
from pydantic import ValidationError

from taskloom.contracts.models import (
    Checkpoint,
    ImageAttachment,
    ModelReply,
    TaskCreate,
    ToolCall,
    Usage,
)
from taskloom.runtime.agent import run_agent
from taskloom.runtime.prompts import GENERAL_PROMPT
from taskloom.runtime.registry import agent_registry


class Host:
    def __init__(self, replies, results=None):
        self.replies = iter(replies)
        self.results = results or {}
        self.checkpoints = []

    async def model(self, messages, round_index):
        return next(self.replies)

    async def tool(self, call, model_round):
        return self.results[call.call_id]

    async def checkpoint(self, checkpoint):
        self.checkpoints.append(checkpoint.model_copy(deep=True))


def reply(content="", calls=()):
    return ModelReply(
        content=content,
        tool_calls=list(calls),
        usage=Usage(input_tokens=1, output_tokens=1, cost_units=2),
    )


def raster():
    output = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(output, format="PNG")
    return output.getvalue()


async def test_chat_keeps_original_message_history_and_natural_reply():
    host = Host([reply("你之前说的是织序。")])
    spec = TaskCreate(mode="chat", message="我之前说的是什么？")
    assert spec.documents == {}
    history = [
        {"role": "user", "content": "名字叫织序"},
        {"role": "assistant", "content": "记住了"},
    ]
    result = await run_agent(
        host,
        "root",
        spec,
        Checkpoint(),
        12,
        agent_registry(),
        history=history,
        workspace="/workspace",
    )
    assert result["content"] == "你之前说的是织序。"
    messages = host.checkpoints[0].messages
    assert messages[1:3] == history
    assert messages[-1] == {"role": "user", "content": spec.message}
    assert messages[0]["content"] == GENERAL_PROMPT + "\n当前工作目录：/workspace"
    assert "本轮启用" not in messages[0]["content"]


@pytest.mark.parametrize("skill", ["notes", "stats"])
async def test_skill_is_explicit_per_turn_and_statistics_have_receipt(skill):
    replies = [reply("整理完毕")]
    results = {}
    if skill == "stats":
        replies.insert(0, reply(calls=[ToolCall(call_id="s", name="file_stats")]))
        results["s"] = {"files": 1, "words": 2}
    host = Host(replies, results)
    result = await run_agent(
        host,
        "root",
        TaskCreate(mode="chat", message="分析资料", skill=skill),
        Checkpoint(),
        12,
        agent_registry(),
    )
    assert "本轮启用「" in host.checkpoints[0].messages[0]["content"]
    assert result["content"] == "整理完毕"
    if skill == "stats":
        assert result["words"] == 2


async def test_image_tool_results_follow_all_tool_responses():
    calls = [
        ToolCall(call_id=key, name="read_file", arguments={"path": key + ".png", "max_bytes": 1024})
        for key in ("a", "b")
    ]
    url = "data:image/png;base64," + base64.b64encode(raster()).decode()
    host = Host(
        [reply(calls=calls), reply("红色")],
        {
            key: {"files": 1, "path": key + ".png", "image_data_url": url + key}
            for key in ("a", "b")
        },
    )
    result = await run_agent(
        host,
        "root",
        TaskCreate(mode="chat", message="查看图片"),
        Checkpoint(),
        12,
        agent_registry(),
    )
    messages = host.checkpoints[-1].messages
    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "tool",
        "user",
        "user",
        "assistant",
    ]
    assert "image_data_url" not in messages[3]["content"]
    assert result["content"] == "红色"


def test_upload_validates_actual_image_and_declared_mime():
    encoded = base64.b64encode(raster()).decode()
    image = ImageAttachment(name="a.png", mime_type="image/png", data_base64=encoded)
    assert TaskCreate(mode="chat", images=[image]).documents == {}
    with pytest.raises(ValidationError):
        ImageAttachment(name="a.jpg", mime_type="image/jpeg", data_base64=encoded)
    with pytest.raises(ValidationError):
        ImageAttachment(
            name="a.png", mime_type="image/png", data_base64=base64.b64encode(b"text").decode()
        )
    with pytest.raises(ValidationError):
        TaskCreate(mode="chat", images=[image, image])
