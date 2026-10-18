"""Loopback-only dashboard server; all external API access stays in one worker."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .core import Monitor

STATIC = Path(__file__).parent / "static"


def create_server(monitor: Monitor, port: int = 8765) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def reply(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def json_reply(self, status: int, data: object) -> None:
            self.reply(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def valid_host(self) -> bool:
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def do_GET(self) -> None:
            if not self.valid_host():
                self.json_reply(403, {"error": "Hôte non autorisé."})
                return
            path = urlsplit(self.path).path
            if path == "/api/state":
                self.json_reply(200, monitor.state())
            elif path == "/api/diagnostic.json":
                state = monitor.state()
                self.json_reply(200, {key: state[key] for key in ("version", "mode", "config", "status", "discord", "clock")})
            elif path == "/api/export.csv":
                self.reply(200, ("\ufeff" + monitor.export_csv()).encode(), "text/csv; charset=utf-8")
            elif path in ("/", "/app.js", "/styles.css"):
                name, kind = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/styles.css": ("styles.css", "text/css")}[path]
                self.reply(200, (STATIC / name).read_bytes(), kind + "; charset=utf-8")
            else:
                self.json_reply(404, {"error": "Introuvable."})

        def do_POST(self) -> None:
            origin = self.headers.get("Origin")
            origins = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            if not self.valid_host() or (origin is not None and origin not in origins):
                self.json_reply(403, {"error": "Origine non autorisée."})
                return
            if self.path not in {"/api/compare", "/api/aleca", "/api/discord/config", "/api/discord/test"}:
                self.json_reply(404, {"error": "Introuvable."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 4096 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("Corps JSON invalide.")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Objet JSON requis.")
                if self.path in {"/api/aleca", "/api/compare"}:
                    if not isinstance(data.get("id"), str):
                        raise ValueError("Identifiant requis.")
                    if data.get("received_at") is not None and not isinstance(data["received_at"], str):
                        raise ValueError("Horodatage invalide.")
                if self.path == "/api/discord/config":
                    monitor.discord.configure(data)
                elif self.path == "/api/discord/test":
                    monitor.discord.test_message()
                elif self.path == "/api/aleca":
                    monitor.record_aleca(data["id"], data.get("price"), data.get("received_at"))
                else:
                    monitor.compare(data["id"], data.get("received_at"), data.get("event_id"))
            except (ValueError, TypeError) as exc:
                self.json_reply(400, {"error": str(exc)})
                return
            self.json_reply(200, monitor.state())

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server
