"""macOS deny-default sandbox profile and non-secret child environment."""

from __future__ import annotations

import shutil
import sys
import subprocess
import json
from functools import lru_cache
from pathlib import Path


def allowed_environment(request_dir: Path, models_dir: Path) -> dict[str, str]:
    request_dir, models_dir = request_dir.resolve(), models_dir.resolve()
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    env.update(
        {
            "TMPDIR": str(request_dir),
            "WHILLY_REQUEST_DIR": str(request_dir),
            "HF_HOME": str(models_dir),
            "HF_HUB_CACHE": str(models_dir / "hub"),
            "FASTEMBED_CACHE_PATH": str(models_dir / "fastembed"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "DATA_ROOT_DIRECTORY": str(request_dir / "cognee-data"),
            "SYSTEM_ROOT_DIRECTORY": str(request_dir / "cognee-system"),
            "CACHE_ROOT_DIRECTORY": str(request_dir / "cognee-cache"),
            "COGNEE_LOGS_DIR": str(request_dir / "logs"),
            "COGNEE_REPOS_DIR": str(request_dir / "repos"),
            "TELEMETRY_DISABLED": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "CACHING": "false",
            "VECTOR_DB_SUBPROCESS_ENABLED": "false",
            "GRAPH_DATABASE_SUBPROCESS_ENABLED": "false",
            "GLINER_AUTO_INSTALL": "false",
            "GRAPH_EXTRACTOR": "gliner_demo",
            "EMBEDDING_PROVIDER": "fastembed",
            "EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5",
            "EMBEDDING_DIMENSIONS": "384",
            "AUTO_FEEDBACK": "false",
            "LOG_LEVEL": "ERROR",
            "COGNEE_TRACING_ENABLED": "false",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "XDG_CACHE_HOME": str(request_dir / "cache"),
            "NETRC": "/dev/null",
        }
    )
    env["WHILLY_MODEL_DIRS"] = str(models_dir)
    return env


def sandbox_command(
    command: list[str], *, runtime: Path, request_dir: Path, model_dirs: tuple[Path, ...], worker_dir: Path
) -> list[str]:
    if sys.platform != "darwin":
        raise RuntimeError("unsupported_os")
    executable = shutil.which("sandbox-exec")
    if executable is None:
        raise RuntimeError("sandbox_unavailable")
    request_dir.mkdir(parents=True, exist_ok=True)
    request_dir = request_dir.resolve()
    request_dir.chmod(0o700)
    profile = request_dir / "sandbox.sb"
    interpreter = Path(command[0]).resolve()
    if not interpreter.is_file():
        raise RuntimeError("runtime_python_missing")
    python_prefix = interpreter.parents[1]
    profile.write_text(
        _profile(runtime, request_dir, model_dirs, worker_dir, interpreter, python_prefix), encoding="utf-8"
    )
    return [executable, "-f", str(profile), *command]


def _profile(
    runtime: Path,
    request_dir: Path,
    model_dirs: tuple[Path, ...],
    worker_dir: Path,
    interpreter: Path,
    python_prefix: Path,
) -> str:
    lines = [
        "(version 1)",
        "(deny default)",
        '(allow file-read* (literal "/"))',
        "(allow process*)",
        "(allow file-read-metadata)",
        "(allow file-map-executable)",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
    ]
    for path in (
        runtime,
        interpreter,
        python_prefix,
        request_dir,
        Path("/usr/lib"),
        Path("/System/Library"),
        Path("/dev/null"),
        Path("/dev/random"),
        Path("/dev/urandom"),
        *model_dirs,
    ):
        lines.append(f"(allow file-read* (subpath {json.dumps(str(path.resolve()))}))")
    for path in _linked_libraries(interpreter, python_prefix):
        lines.append(f"(allow file-read* (literal {json.dumps(str(path))}))")
    worker = (worker_dir / "cognee_worker.py").resolve()
    lines.append(f"(allow file-read* (literal {json.dumps(str(worker))}))")
    lines.append(f"(allow file-read* (literal {json.dumps(str(Path('/usr/share/zoneinfo/UTC').resolve()))}))")
    lines.append(f"(allow file-write* (subpath {json.dumps(str(request_dir))}))")
    lines.append("(deny network*)")
    return "\n".join(lines) + "\n"


@lru_cache(maxsize=8)
def _linked_libraries(interpreter: Path, prefix: Path) -> tuple[Path, ...]:
    """Read exact external dylib dependencies of Python and its stdlib extensions."""
    pending = [interpreter, *prefix.glob("lib/python*/lib-dynload/*.so")]
    seen: set[Path] = set()
    libraries: set[Path] = set()
    while pending:
        binary = pending.pop().resolve()
        if binary in seen:
            continue
        seen.add(binary)
        result = subprocess.run(["/usr/bin/otool", "-L", str(binary)], capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise RuntimeError("runtime_dependencies_unavailable")
        for line in result.stdout.splitlines()[1:]:
            name = line.strip().split(" (", 1)[0]
            if not name.startswith("/") or name.startswith(("/usr/lib/", "/System/Library/")):
                continue
            path = Path(name).resolve(strict=True)
            libraries.add(path)
            pending.append(path)
    return tuple(sorted(libraries))
