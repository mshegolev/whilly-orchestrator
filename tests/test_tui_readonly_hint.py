from whilly.cli.tui import TuiState, _read_only_hint


def test_hint_present_in_read_only():
    state = TuiState()
    state.read_only = True
    assert "read-only" in _read_only_hint(state).lower()


def test_no_hint_in_db_mode():
    state = TuiState()
    state.read_only = False
    assert _read_only_hint(state) == ""
