from taskloom.bootstrap.development import self_test


def test_real_two_endpoint_workflow(tmp_path) -> None:
    result = self_test(tmp_path / "system")
    assert result["self_test"] == "passed"
    assert result["words"] == 5
