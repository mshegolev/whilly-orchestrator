import pytest

from whilly.cli.tui import resolve_backend_spec


def test_connect_url_selects_http():
    spec = resolve_backend_spec(connect="https://whilly.corp", token="t", insecure=False, dsn=None)
    assert spec.kind == "http"
    assert spec.base_url == "https://whilly.corp"


def test_dsn_selects_db_when_no_connect():
    spec = resolve_backend_spec(connect=None, token=None, insecure=False, dsn="postgresql://x")
    assert spec.kind == "db"


def test_neither_is_error():
    with pytest.raises(ValueError):
        resolve_backend_spec(connect=None, token=None, insecure=False, dsn=None)


def test_connect_takes_precedence_over_dsn():
    spec = resolve_backend_spec(connect="https://whilly.corp", token="t", insecure=False, dsn="postgresql://x")
    assert spec.kind == "http"
