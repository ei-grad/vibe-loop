"""Local read-only WebUI for autopilot status.

Built on the standard-library HTTP server so the core package keeps zero runtime
dependencies. The server binds to localhost by default, only answers GET for a
small status page and JSON API, and never authors tasks, merges, serves log file
contents, or exposes raw commands or secrets — it renders the same redacted
``AggregateProjectStatus`` payload as ``autopilot status --json``.
"""

from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from vibe_loop.autopilot import collect_autopilot_results

StatusProvider = Callable[[], dict[str, object]]

DEFAULT_WEBUI_HOST = "127.0.0.1"
DEFAULT_WEBUI_PORT = 8765


def autopilot_status_payload(
    *,
    repo: Path | None = None,
    registry_path: Path | None = None,
    generated_at: str = "",
) -> dict[str, object]:
    results = collect_autopilot_results(repo=repo, registry_path=registry_path)
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "mode": "registry" if registry_path is not None else "repo",
        "projects": [result.to_json() for result in results],
    }


def render_status_page() -> str:
    """Return a self-contained HTML page that polls the JSON API.

    The page holds no project data itself; it fetches ``/api/status`` so the
    server stays the single source of truth and nothing is scraped from text.
    """

    title = html.escape("vibe-loop autopilot")
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        f"<title>{title}</title>\n"
        "<style>body{font-family:system-ui,sans-serif;margin:1.5rem}"
        "table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #ccc;padding:.35rem .5rem;text-align:left;"
        "font-size:.9rem}th{background:#f3f3f3}caption{text-align:left;"
        "margin-bottom:.5rem;color:#555}.err{color:#b00}</style>\n"
        "</head><body>\n"
        f"<h1>{title}</h1>\n"
        '<p id="meta"></p>\n'
        '<table id="projects"><thead><tr>'
        "<th>Project</th><th>Repo</th><th>Queue</th><th>Workers</th>"
        "<th>Supervisor</th><th>Log</th><th>Last cycle</th><th>Blockers</th>"
        "<th>Next wake</th></tr></thead><tbody></tbody></table>\n"
        "<script>\n"
        "async function refresh(){\n"
        " const r=await fetch('api/status');const d=await r.json();\n"
        " document.getElementById('meta').textContent="
        "'mode: '+d.mode+' \\u2014 '+d.projects.length+' project(s)';\n"
        " const b=document.querySelector('#projects tbody');b.innerHTML='';\n"
        " for(const p of d.projects){\n"
        "  const s=p.status;const tr=document.createElement('tr');\n"
        "  const cells=[p.name,p.repo];\n"
        "  if(!s){cells.push('\\u2014','\\u2014','\\u2014','\\u2014','\\u2014',"
        "'error: '+p.error,'\\u2014');}\n"
        "  else{const q=s.queue;const wk=(s.workers||[]).filter("
        "w=>w.state==='running').length;const sup=s.supervisor||{};\n"
        "   const lc=s.last_cycle;\n"
        "   cells.push(q.source_error?'unavailable':(q.runnable+'/'+q.total+' ready'),"
        "String(wk),(sup.state||'')+(sup.pid?(' pid='+sup.pid):''),"
        "(sup.log||'\\u2014'),(lc?(lc.cycle_id+': '+lc.status):'\\u2014'),"
        "((s.blockers||[]).join(', ')||'none'),(s.next_wake||'\\u2014'));}\n"
        "  for(const c of cells){const td=document.createElement('td');"
        "td.textContent=c;tr.appendChild(td);}b.appendChild(tr);}\n"
        "}\n"
        "refresh();setInterval(refresh,5000);\n"
        "</script>\n"
        "</body></html>\n"
    )


def status_response(
    path: str,
    status_provider: StatusProvider,
) -> tuple[int, str, bytes]:
    """Resolve a GET path to (http_status, content_type, body).

    Pure routing helper so the surface is testable without a socket. Only the
    page and the JSON API are served; everything else is 404.
    """

    route = path.split("?", 1)[0].rstrip("/") or "/"
    if route == "/":
        return 200, "text/html; charset=utf-8", render_status_page().encode("utf-8")
    if route == "/api/status":
        body = json.dumps(status_provider(), default=str).encode("utf-8")
        return 200, "application/json", body
    return 404, "text/plain; charset=utf-8", b"not found"


def make_handler(status_provider: StatusProvider) -> type[BaseHTTPRequestHandler]:
    class AutopilotWebHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
            code, content_type, body = status_response(self.path, status_provider)
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            # Silence default stderr request logging; the WebUI is a status view.
            return

    return AutopilotWebHandler


def build_server(
    status_provider: StatusProvider,
    *,
    host: str = DEFAULT_WEBUI_HOST,
    port: int = DEFAULT_WEBUI_PORT,
) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(status_provider))


def run_webui(
    *,
    repo: Path | None = None,
    registry_path: Path | None = None,
    host: str = DEFAULT_WEBUI_HOST,
    port: int = DEFAULT_WEBUI_PORT,
    on_serving: Callable[[ThreadingHTTPServer], None] | None = None,
) -> None:
    def provider() -> dict[str, object]:
        from vibe_loop.runs import utc_now_iso

        return autopilot_status_payload(
            repo=repo, registry_path=registry_path, generated_at=utc_now_iso()
        )

    server = build_server(provider, host=host, port=port)
    if on_serving is not None:
        on_serving(server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
