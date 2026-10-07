import pytest

from whilly.swarm.context import bounded_history


def test_bounded_history_keeps_newest_user_request():
    history = [{"sender": "planner", "body": "old"}, {"sender": "user", "body": "new request"}]
    assert bounded_history(history, max_chars=11)[-1]["body"] == "new request"


def test_oversize_newest_request_is_explicit():
    with pytest.raises(ValueError, match="latest user request"):
        bounded_history([{"sender": "user", "body": "12345"}], max_chars=4)


def test_bounded_history_preserves_chronology_and_marks_omissions():
    history = [
        {"sender": "user", "body": "request"},
        {"sender": "planner", "body": "a" * 20},
        {"sender": "planner", "body": "later"},
    ]
    result = bounded_history(history, max_chars=14)
    assert [item["body"] for item in result] == [
        "request",
        "[1 earlier message(s) omitted for context budget]",
        "later",
    ]
