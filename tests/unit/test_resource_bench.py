from taskloom.experiments.resource_bench import protection_trial


async def test_paired_dispatcher_trials_preserve_work_and_resource_bounds():
    trials = [await protection_trial(enabled) for enabled in (False, True)]
    assert trials[0]["workload_hash"] == trials[1]["workload_hash"]
    for trial in trials:
        assert trial["completed"] == trial["submitted"] == 81
        assert trial["peak_memory_units"] <= trial["capacity_memory_units"]
        assert trial["allocations_remaining"] == 0
        assert trial["large_wait_seconds"] >= 0
        assert trial["light_p95_wait_seconds"] >= 0
