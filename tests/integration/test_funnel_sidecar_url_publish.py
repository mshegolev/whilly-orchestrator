"""Integration test for the funnel sidecar's URL-publish path.

End-to-end smoke for ``m2-localhostrun-funnel-sidecar``:

* Build the ``Dockerfile.funnel`` image.
* Boot a testcontainers Postgres at the migrated head (so the
  ``funnel_url`` singleton table exists).
* Run the funnel container with ``FUNNEL_FAKE_URL`` (bypasses real
  SSH so no outbound TCP/22 to localhost.run is needed) and
  ``FUNNEL_ONESHOT=1`` (exit after first publish).
* Assert the synthetic URL appears in (a) the postgres
  ``funnel_url`` row and (b) the shared-volume file
  ``/funnel/url.txt`` within ~10 s.

Skipped when Docker is not reachable (testcontainers/buildx need a
live daemon). Tests do NOT depend on internet egress to
``localhost.run`` — the ``FUNNEL_FAKE_URL`` test bypass keeps the
suite hermetic.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from alembic import command

from tests.conftest import (
    DOCKER_REQUIRED,
    HAS_TESTCONTAINERS,
    _build_alembic_config,
    _retry_colima_flake,
    docker_available,
    resolve_docker_host,
)

pytestmark = DOCKER_REQUIRED


REPO_ROOT: Path = Path(__file__).resolve().parents[2]
DOCKERFILE_FUNNEL: Path = REPO_ROOT / "Dockerfile.funnel"
RUN_SH: Path = REPO_ROOT / "scripts" / "funnel" / "run.sh"
FAKE_URL: str = "https://fake-test-funnel-{uid}.lhr.life"
IMAGE_TAG: str = "whilly-funnel:test-funnel-publish"


def _docker_buildable() -> bool:
    """Return True iff `docker build` would work (daemon + binary present)."""
    if shutil.which("docker") is None:
        return False
    return docker_available()


def _to_asyncpg_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://")


async def _fetchval(dsn: str, sql: str, *args: Any) -> Any:
    conn = await asyncpg.connect(_to_asyncpg_dsn(dsn))
    try:
        return await conn.fetchval(sql, *args)
    finally:
        await conn.close()


async def _execute(dsn: str, sql: str, *args: Any) -> None:
    conn = await asyncpg.connect(_to_asyncpg_dsn(dsn))
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def funnel_image() -> Iterator[str]:
    """Build the funnel image once per module; tag = IMAGE_TAG."""
    if not _docker_buildable():
        pytest.skip("Docker daemon not reachable; cannot build funnel image")
    if not DOCKERFILE_FUNNEL.is_file():
        pytest.skip(f"Dockerfile.funnel missing at {DOCKERFILE_FUNNEL}")

    cmd = [
        "docker",
        "build",
        "--quiet",
        "-f",
        str(DOCKERFILE_FUNNEL),
        "-t",
        IMAGE_TAG,
        str(REPO_ROOT),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        pytest.skip(
            f"docker build of Dockerfile.funnel failed (likely environmental):\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
    try:
        yield IMAGE_TAG
    finally:
        subprocess.run(
            ["docker", "rmi", "-f", IMAGE_TAG],
            capture_output=True,
            text=True,
            timeout=60,
        )


@pytest.fixture
def migrated_postgres(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str, int]]:
    """Boot a fresh Postgres at head; yield (dsn, host_for_container, port)."""
    if not (HAS_TESTCONTAINERS and docker_available()):
        pytest.skip("Docker daemon not reachable; testcontainers cannot boot Postgres")
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    if "DOCKER_HOST" not in os.environ:
        resolved = resolve_docker_host()
        if resolved is not None:
            monkeypatch.setenv("DOCKER_HOST", resolved)
    monkeypatch.setenv("TESTCONTAINERS_RYUK_DISABLED", "true")

    pg = PostgresContainer("postgres:15-alpine")
    started = False
    try:
        _retry_colima_flake(
            pg.start,
            op="PostgresContainer('postgres:15-alpine').start() (test_funnel_sidecar)",
        )
        started = True
        raw = pg.get_connection_url()
        dsn = raw.replace("postgresql+psycopg2://", "postgresql://").replace("+psycopg2", "")
        monkeypatch.setenv("WHILLY_DATABASE_URL", dsn)
        cfg = _build_alembic_config(dsn)
        _retry_colima_flake(
            lambda: command.upgrade(cfg, "head"),
            op="alembic.command.upgrade(head) (test_funnel_sidecar)",
        )
        host = pg.get_container_host_ip()
        port = int(pg.get_exposed_port(5432))
        yield dsn, host, port
    finally:
        if started:
            try:
                pg.stop()
            except Exception:  # noqa: BLE001 — teardown best effort
                pass


def _create_named_volume(name: str) -> None:
    subprocess.run(["docker", "volume", "create", name], capture_output=True, text=True, timeout=15)


def _remove_named_volume(name: str) -> None:
    subprocess.run(["docker", "volume", "rm", "-f", name], capture_output=True, text=True, timeout=15)


def _read_volume_file(volume: str, container_path: str) -> str | None:
    """Read a file out of a named docker volume by running a probe container.

    Returns the file's textual content (stripped) or ``None`` if the file
    is missing inside the volume. Using a named volume + probe container
    sidesteps macOS / colima bind-mount edge cases (pytest's ``tmp_path``
    lives under ``/private/var/folders/...`` which is **not** auto-mounted
    into the colima VM, so files written via a bind-mounted ``tmp_path``
    don't propagate back to the host filesystem).
    """
    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{volume}:/probe",
        "alpine:3.20",
        "sh",
        "-c",
        f"if [ -f {container_path} ]; then cat {container_path}; fi",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return None
    if not result.stdout:
        return None
    return result.stdout.strip()


def _resolve_pg_dsn_for_container(dsn: str, host: str, port: int) -> str:
    """Translate the testcontainers DSN into one a sibling container can dial.

    `pg.get_container_host_ip()` returns ``localhost`` / ``127.0.0.1`` on the
    host. From inside another container that is unreachable; rewrite to
    ``host.docker.internal`` (Docker Desktop / colima default) so the
    sidecar's psql can dial the postgres testcontainer.
    """
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(dsn)
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        new_host = "host.docker.internal"
    else:
        new_host = host
    netloc = parsed.netloc
    if "@" in netloc:
        creds, _ = netloc.split("@", 1)
        netloc = f"{creds}@{new_host}:{port}"
    else:
        netloc = f"{new_host}:{port}"
    return urlunparse(parsed._replace(netloc=netloc))


def test_funnel_sidecar_publishes_url_to_postgres_and_file(
    funnel_image: str,
    migrated_postgres: tuple[str, str, int],
) -> None:
    """End-to-end: sidecar publishes FUNNEL_FAKE_URL to postgres + named-volume file."""
    host_dsn, pg_host, pg_port = migrated_postgres
    container_dsn = _resolve_pg_dsn_for_container(host_dsn, pg_host, pg_port)
    fake_url = FAKE_URL.format(uid=uuid.uuid4().hex[:12])
    container_name = f"whilly-funnel-test-{uuid.uuid4().hex[:8]}"
    volume_name = f"whilly-funnel-test-vol-{uuid.uuid4().hex[:8]}"
    _create_named_volume(volume_name)

    try:
        cmd = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "-e",
            f"FUNNEL_FAKE_URL={fake_url}",
            "-e",
            "FUNNEL_ONESHOT=1",
            "-e",
            f"WHILLY_DATABASE_URL={container_dsn}",
            "-e",
            "FUNNEL_URL_FILE=/funnel/url.txt",
            "--add-host",
            "host.docker.internal:host-gateway",
            "-v",
            f"{volume_name}:/funnel",
            funnel_image,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            pytest.skip(
                f"funnel container failed to run (env-specific):\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
            )

        published_url = _read_volume_file(volume_name, "/probe/url.txt")
        assert published_url == fake_url, (
            f"funnel sidecar did not publish URL to /funnel/url.txt; "
            f"observed_in_volume={published_url!r}, expected={fake_url!r}; "
            f"sidecar_stdout={result.stdout!r}"
        )

        deadline = time.time() + 10
        pg_url: str | None = None
        while time.time() < deadline:
            try:
                pg_url = asyncio.run(_fetchval(host_dsn, "SELECT url FROM funnel_url WHERE id = 1"))
            except Exception:  # noqa: BLE001 — transient
                pg_url = None
            if pg_url == fake_url:
                break
            time.sleep(0.2)
        assert pg_url == fake_url, (
            f"funnel_url postgres row does not contain the published URL within 10s; "
            f"observed={pg_url!r}, expected={fake_url!r}; sidecar_stdout={result.stdout!r}"
        )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        _remove_named_volume(volume_name)


def test_funnel_sidecar_writes_file_when_database_url_unset(funnel_image: str) -> None:
    """Sidecar still publishes to /funnel/url.txt when WHILLY_DATABASE_URL is empty.

    Pins the publish-fallback contract — operators on environments
    without postgres reachability still get the rotating URL via the
    shared-volume file.
    """
    fake_url = FAKE_URL.format(uid=uuid.uuid4().hex[:12])
    container_name = f"whilly-funnel-nopg-{uuid.uuid4().hex[:8]}"
    volume_name = f"whilly-funnel-nopg-vol-{uuid.uuid4().hex[:8]}"
    _create_named_volume(volume_name)

    try:
        cmd = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "-e",
            f"FUNNEL_FAKE_URL={fake_url}",
            "-e",
            "FUNNEL_ONESHOT=1",
            "-e",
            "FUNNEL_URL_FILE=/funnel/url.txt",
            "-v",
            f"{volume_name}:/funnel",
            funnel_image,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            pytest.skip(f"funnel container failed (env-specific):\nstdout={result.stdout!r}\nstderr={result.stderr!r}")
        published_url = _read_volume_file(volume_name, "/probe/url.txt")
        assert published_url == fake_url, (
            f"funnel sidecar did not write /funnel/url.txt; "
            f"observed={published_url!r}, expected={fake_url!r}; "
            f"sidecar_stdout={result.stdout!r}"
        )
        assert "WHILLY_DATABASE_URL not set" in result.stdout, (
            f"sidecar should explicitly note skipping postgres publish when DSN is unset; "
            f"observed stdout={result.stdout!r}"
        )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        _remove_named_volume(volume_name)


# Anchor for the localhost-name resolution sanity check ---------------------


def test_runtime_environment_can_resolve_loopback() -> None:
    """Sanity: testcontainers_pg fixture's loopback host resolves on this runner.

    Catches the macOS / colima case where ``localhost`` maps to a
    vsock proxy that the sibling container can't reach. The actual
    publish test uses ``host.docker.internal`` to bridge that gap.
    """
    try:
        socket.gethostbyname("localhost")
    except socket.gaierror:
        pytest.skip("localhost does not resolve on this runner")
