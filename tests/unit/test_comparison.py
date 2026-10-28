import pytest

from taskloom.experiments.comparison import ComparisonSpec, Stage, Task, _Run, compare


def test_sequential_stage_timing_and_independent_personal_resources():
    spec = ComparisonSpec(days=1, tasks_per_day=8, model_slots=2, tool_slots=1, repeats=1)
    tasks = [
        Task(
            f"task-{i}",
            f"user-{i}",
            "interactive",
            10,
            20,
            (Stage("model", 1), Stage("tool", 3, 384), Stage("model", 2)),
        )
        for i in range(2)
    ]
    for policy in ("fifo", "round_robin", "taskloom"):
        result = _Run(spec, tasks, policy).run()
        assert result["metrics"]["completed_tasks"] == 2
        assert result["metrics"]["interactive_mean_seconds"] == 6
        assert result["metrics"]["on_time_rate"] == 1
        assert result["metrics"]["peak_tool_slots_per_user"] == 1
        assert result["metrics"]["total_service_seconds"] == 12


def test_paired_work_conservation_resource_bounds_and_reproducibility():
    spec = ComparisonSpec(days=2, tasks_per_day=24, repeats=2, memory_mib=384, seed=7)
    result = compare(spec)
    assert result == compare(spec)
    for seed in (7, 8):
        runs = [run for run in result["runs"] if run["seed"] == seed]
        assert len({run["workload_hash"] for run in runs}) == 1
        work = [run["metrics"]["total_service_seconds"] for run in runs]
        assert max(work) == pytest.approx(min(work))
        for run in runs:
            m = run["metrics"]
            assert m["submitted_tasks"] == m["completed_tasks"] == 48
            assert m["peak_model_slots"] <= spec.model_slots
            assert m["peak_tool_slots_per_user"] <= spec.tool_slots
            assert m["peak_memory_mib_per_user"] <= spec.memory_mib
            assert len(run["trace"]) <= result["trace_limit_per_run"]
            assert 0 <= m["on_time_rate"] <= 1


def test_improvement_keeps_negative_values_instead_of_hiding_regressions():
    result = compare(ComparisonSpec(days=1, tasks_per_day=24, repeats=1, seed=7))
    base = result["summary"][0]["metrics"]["background_max_wait_seconds"]["mean"]
    proposed = result["summary"][-1]["metrics"]["background_max_wait_seconds"]["mean"]
    assert result["improvements_percent"]["fifo"]["background_max_wait_seconds"] == pytest.approx(
        100 * (base - proposed) / base
    )


def test_experiment_work_is_bounded():
    with pytest.raises(ValueError, match="18000"):
        ComparisonSpec(days=30, tasks_per_day=200, repeats=5)
