from ci_repair.context import failure_evidence


def test_early_error_survives_long_cleanup_tail_with_source_lines():
    log = '\x1b[31mTraceback (most recent call last):\x1b[0m\n  File "src/app.py", line 42\nValueError: broken\n'
    log += "cleanup\n" * 4000
    evidence = failure_evidence(log)
    assert evidence["raw_truncated"]
    assert "ValueError: broken" in evidence["error_blocks"][0]["text"]
    assert "\x1b" not in evidence["error_blocks"][0]["text"]
    assert evidence["error_blocks"][0]["start_line"] == 1
    assert evidence["locations"] == [{"path": "src/app.py", "line": 42, "log_line": 2}]


def test_unknown_format_and_template_injection_remain_raw_data():
    log = "{{execute('rm -rf /')}}\nno recognized diagnostic"
    evidence = failure_evidence(log)
    assert evidence["raw_excerpt"] == log
    assert evidence["error_blocks"] == []
    assert evidence["locations"] == []


def test_repeated_diagnostics_are_deduplicated_and_bounded():
    evidence = failure_evidence(("src/app.py:12: error: " + "x" * 1000 + "\n") * 10000)
    assert len(evidence["locations"]) == 1
    assert sum(len(b["text"]) for b in evidence["error_blocks"]) <= 7000
    assert len(evidence["raw_excerpt"]) < 6100
    assert len(evidence["error_blocks"]) <= 8
    assert any(b["truncated"] for b in evidence["error_blocks"])


def test_baseline_evidence_overlap_ignores_runner_paths_and_timestamps():
    from ci_repair.context import evidence_overlap

    ci = (
        '2026-09-18T04:28:19.1Z   File "/home/runner/work/r/r/src/a.py", line 3\n'
        "2026-09-18T04:28:19.1Z AssertionError: expected 14, got 9\n"
    )
    assert evidence_overlap(ci, "AssertionError: expected 14, got 9\n") is True
    assert evidence_overlap(ci, "ModuleNotFoundError: No module named 'x'\n") is False
    assert evidence_overlap("all good\n", "anything") is None
