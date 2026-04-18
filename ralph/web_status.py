"""Lightweight HTTP status endpoint for Ralph orchestrator.

Provides ``/api/status`` (JSON) and ``/`` (HTML) on localhost:9191.
Uses only stdlib — no FastAPI/Flask dependencies.

Usage:
    from ralph.web_status import WebStatusServer
    server = WebStatusServer(port=9191)
    server.start()              # daemon thread
    server.update(done=5, total=10, cost_usd=1.23, agents=[...])
    server.stop()
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

log = logging.getLogger("ralph.web")

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Ralph Status</title>
<meta http-equiv="refresh" content="5">
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; background: #1a1a2e; color: #e0e0e0; }}
  h1 {{ color: #00d4aa; }}
  .progress {{ background: #333; border-radius: 8px; overflow: hidden; height: 24px; margin: 16px 0; }}
  .progress-bar {{ background: linear-gradient(90deg, #00d4aa, #00b894); height: 100%; transition: width 0.5s; display: flex; align-items: center; justify-content: center; color: #1a1a2e; font-weight: bold; }}
  .stat {{ display: inline-block; background: #2d2d44; padding: 12px 20px; border-radius: 8px; margin: 4px; }}
  .stat .value {{ font-size: 24px; font-weight: bold; color: #00d4aa; }}
  .stat .label {{ font-size: 12px; color: #888; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
  th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #333; }}
  th {{ color: #888; font-size: 12px; text-transform: uppercase; }}
  .status-done {{ color: #00d4aa; }} .status-running {{ color: #ffd93d; }} .status-failed {{ color: #ff6b6b; }}
  footer {{ margin-top: 32px; color: #555; font-size: 12px; }}
</style>
</head>
<body>
<h1>Ralph Orchestrator</h1>
<div class="progress"><div class="progress-bar" style="width:{pct}%">{pct:.0f}%</div></div>
<div>
  <span class="stat"><span class="value">{done}</span><br><span class="label">Done</span></span>
  <span class="stat"><span class="value">{failed}</span><br><span class="label">Failed</span></span>
  <span class="stat"><span class="value">{total}</span><br><span class="label">Total</span></span>
  <span class="stat"><span class="value">${cost_usd:.2f}</span><br><span class="label">Cost</span></span>
  <span class="stat"><span class="value">{elapsed}</span><br><span class="label">Elapsed</span></span>
</div>
{agents_html}
<footer>Auto-refreshes every 5s &middot; Updated {updated}</footer>
</body>
</html>
"""


class _StatusState:
    """Thread-safe status state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "done": 0,
            "total": 0,
            "failed": 0,
            "cost_usd": 0.0,
            "elapsed_sec": 0,
            "agents": [],
            "plan": "",
        }
        self._start_time = datetime.now(timezone.utc)

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            self._data.update(kwargs)

    def get_json(self) -> str:
        with self._lock:
            data = dict(self._data)
        data["updated"] = datetime.now(timezone.utc).isoformat()
        return json.dumps(data, ensure_ascii=False)

    def get_html(self) -> str:
        with self._lock:
            d = dict(self._data)
        pct = (d["done"] / max(d["total"], 1)) * 100
        elapsed_sec = d.get("elapsed_sec", 0)
        elapsed = f"{elapsed_sec // 60}m {elapsed_sec % 60}s"
        agents_html = ""
        if d.get("agents"):
            rows = "".join(
                f"<tr><td>{a.get('task_id','?')}</td>"
                f"<td class='status-running'>{a.get('status','running')}</td>"
                f"<td>{a.get('elapsed_sec',0):.0f}s</td></tr>"
                for a in d["agents"]
            )
            agents_html = f"<h3>Active Agents</h3><table><tr><th>Task</th><th>Status</th><th>Time</th></tr>{rows}</table>"
        return _HTML_TEMPLATE.format(
            pct=pct,
            done=d["done"],
            failed=d["failed"],
            total=d["total"],
            cost_usd=d["cost_usd"],
            elapsed=elapsed,
            agents_html=agents_html,
            updated=datetime.now().strftime("%H:%M:%S"),
        )


_state = _StatusState()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/api/status":
            body = _state.get_json().encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
        elif self.path == "/" or self.path == "/index.html":
            body = _state.get_html().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            body = b"Not Found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # suppress access logs


class WebStatusServer:
    """HTTP server for Ralph status on localhost."""

    def __init__(self, port: int = 9191):
        self._port = port
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start HTTP server in a daemon thread."""
        try:
            self._server = HTTPServer(("127.0.0.1", self._port), _Handler)
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            log.info("Web status server started on http://localhost:%d", self._port)
        except OSError as e:
            log.warning("Could not start web server on port %d: %s", self._port, e)

    def update(self, **kwargs: Any) -> None:
        """Update status data (thread-safe)."""
        _state.update(**kwargs)

    def stop(self) -> None:
        """Stop the HTTP server."""
        if self._server:
            self._server.shutdown()
            log.info("Web status server stopped")
