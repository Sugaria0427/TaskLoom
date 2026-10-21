import pytest

from taskloom.contracts.models import ToolRun
from taskloom.execution.catalog import max_demand, tool_demand, tool_names


def test_catalog_declares_three_distinct_tools() -> None:
    assert set(tool_names()) == {"file_stats", "list_files", "top_words"}


def test_profiles_span_light_to_heavy() -> None:
    light = tool_demand("list_files")
    baseline = tool_demand("file_stats")
    heavy = tool_demand("top_words")
    assert light.memory_bytes < baseline.memory_bytes < heavy.memory_bytes
    assert light.artifact_bytes < heavy.artifact_bytes


def test_max_demand_covers_every_profile() -> None:
    ceiling = max_demand()
    for name in tool_names():
        assert tool_demand(name).fits(ceiling)


def test_tool_run_accepts_every_catalog_tool() -> None:
    for name in tool_names():
        run = ToolRun(
            attempt_id="a",
            root_task_id="r",
            tenant_id="t",
            generation=0,
            tool_name=name,
            input_version="v",
        )
        assert run.tool_name == name


def test_unknown_tool_name_raises() -> None:
    with pytest.raises(ValueError, match="Unknown tool"):
        tool_demand("nope")
