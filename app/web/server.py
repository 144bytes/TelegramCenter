"""The local HTTP server.

Standard library only — no web framework. A thread per request, and the
Telegram work is handed to the asyncio loop that already exists for Telethon.

Nothing here is reachable from the network: the socket is bound to 127.0.0.1
and every /api/ call must present the runtime token.
"""
from __future__ import annotations

import json
import mimetypes
import shutil
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import config
from ..logging import LOG
from .api import GET_ROUTES, POST_ROUTES, UPLOAD_ROUTES, ApiError, Ctx
from .security import TOKEN_HEADER, Guard

MOD = "web"
MAX_BODY = 4 * 1024 * 1024
# Attachments do not travel as JSON: base64 would inflate them by a third and
# hold the whole file in memory twice. The raw body is streamed to a temp file
# instead, so this cap is about staying sane rather than about memory.
MAX_UPLOAD = 64 * 1024 * 1024
UPLOAD_CHUNK = 256 * 1024
HEARTBEAT_SEC = 15.0

_FALLBACK_PAGE = """<!doctype html>
<meta charset="utf-8"><title>TelegramCenter</title>
<style>body{background:#1b1b1a;color:#eceae5;font:14px system-ui;padding:40px}
code{background:#2c2b29;padding:2px 6px;border-radius:4px}</style>
<h1>Интерфейс не собран</h1>
<p>Каталог <code>web/dist</code> пуст. Соберите фронтенд:</p>
<p><code>cd web &amp;&amp; npm ci &amp;&amp; npm run build</code></p>
"""


def find_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TelegramCenter"
    sys_version = ""

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):    # noqa: A003 - silence stderr spam
        pass

    @property
    def app(self) -> "WebServer":
        return self.server.app  # type: ignore[attr-defined]

    def _send(self, status: int, payload, content_type="application/json"):
        if content_type.startswith("application/json"):
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        else:
            data = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, status: int, message: str):
        self._send(status, {"error": message})

    def _guard(self, path: str) -> bool:
        allowed, reason = self.app.guard.check(self.headers, self.command, path)
        if not allowed:
            LOG.warning(f"refused {self.command} {path}: {reason}", module=MOD)
            self._error(403, "forbidden")
            return False
        return True

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return {}
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ApiError("Слишком большой запрос", status=413)
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("Некорректный JSON") from exc
        return data if isinstance(data, dict) else {}

    # -- verbs ------------------------------------------------------------
    def do_OPTIONS(self):  # noqa: N802
        # Refusing the preflight is what stops any other page from calling the
        # API with the custom token header.
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._guard(path):
            return
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if path == "/api/events":
            self._stream_events()
            return
        handler = GET_ROUTES.get(path)
        if handler is not None:
            self._call(handler, {}, query)
            return
        if path.startswith("/api/"):
            self._error(404, "not found")
            return
        self._serve_static(path)

    # -- uploads ----------------------------------------------------------
    def _upload(self, handler) -> None:
        """Stream the raw request body to a temp file and hand over the path.

        Metadata rides in headers rather than the query string: a caption is
        something a person wrote, and user content has no business in a URL.
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(400, "Некорректный запрос")
            return
        if length <= 0:
            self._error(400, "Пустой файл")
            return
        if length > MAX_UPLOAD:
            self._error(413, f"Файл больше {MAX_UPLOAD // (1024 * 1024)} МБ")
            return

        meta = {
            "id": unquote(self.headers.get("X-TC-Operator", "")),
            "peer_id": unquote(self.headers.get("X-TC-Peer", "")),
            "name": unquote(self.headers.get("X-TC-Name", "")) or "file",
            "caption": unquote(self.headers.get("X-TC-Caption", "")),
            "reply_to": self.headers.get("X-TC-Reply-To", ""),
        }
        # Telegram names the file after what is on disk, so the temp copy keeps
        # the original name inside a directory of its own.
        folder = Path(tempfile.mkdtemp(prefix="tc-upload-"))
        # Only the base name is kept, and a name that is nothing but dots would
        # resolve outside the folder - so anything that does not survive as a
        # plain filename falls back to one we choose.
        safe = Path(meta["name"]).name
        if safe in ("", ".", "..") or "/" in safe or "\\" in safe:
            safe = "upload"
        target = folder / safe
        try:
            remaining = length
            with target.open("wb") as fh:
                while remaining > 0:
                    chunk = self.rfile.read(min(UPLOAD_CHUNK, remaining))
                    if not chunk:
                        break
                    fh.write(chunk)
                    remaining -= len(chunk)
            if remaining > 0:
                self._error(400, "Файл передан не полностью")
                return
            meta["path"] = str(target)
            self._call(handler, meta, {})
        finally:
            # The file has already been handed to Telegram by now; keeping it
            # would quietly fill the disk one attachment at a time. rmtree
            # rather than unlink + rmdir: on Windows the directory sometimes
            # refuses to go on the first try, and a retry clears it.
            shutil.rmtree(folder, ignore_errors=True)
            if folder.exists():
                shutil.rmtree(folder, ignore_errors=True)
            if folder.exists():
                LOG.warning(f"upload temp folder left behind: {folder}", module=MOD)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if not self._guard(path):
            return
        upload = UPLOAD_ROUTES.get(path)
        if upload is not None:
            self._upload(upload)
            return
        handler = POST_ROUTES.get(path)
        if handler is None:
            self._error(404, "not found")
            return
        try:
            body = self._body()
        except ApiError as exc:
            self._error(exc.status, exc.message)
            return
        self._call(handler, body, {})

    def _call(self, handler, body, query):
        try:
            result = handler(self.app.ctx, body, query)
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message})
            return
        except Exception as exc:  # noqa: BLE001
            LOG.error(f"{self.path}: {type(exc).__name__}: {exc}", module=MOD)
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._send(200, result)

    # -- SSE --------------------------------------------------------------
    def _stream_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        sub = self.app.bus.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            for event in sub.listen(heartbeat=HEARTBEAT_SEC):
                if event is None:
                    chunk = b": ping\n\n"
                else:
                    data = json.dumps(event.to_dict(), ensure_ascii=False, default=str)
                    chunk = f"data: {data}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass          # the tab was closed; that is normal, not an error
        finally:
            sub.close()

    # -- static -----------------------------------------------------------
    def _serve_static(self, path: str):
        root: Path = config.WEB_DIR
        if not root.is_dir():
            self._send(200, _FALLBACK_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return

        rel = path.lstrip("/") or "index.html"
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            self._error(403, "forbidden")      # no escaping the bundle directory
            return

        if not candidate.is_file():
            candidate = root / "index.html"    # SPA fallback
            if not candidate.is_file():
                self._send(200, _FALLBACK_PAGE.encode("utf-8"),
                           "text/html; charset=utf-8")
                return

        ctype = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                  "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, candidate.read_bytes(), ctype)


class WebServer:
    def __init__(self, services, storage, telegram):
        self.services = services
        self.storage = storage
        self.telegram = telegram
        self.bus = services.bus
        self.host = config.HOST
        self.port = find_free_port(self.host)
        self.guard = Guard(self.host, self.port)
        self.ctx = Ctx(services, storage, self._run_coro)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _run_coro(self, coro, timeout: float = 60):
        """Bridge from an HTTP thread into the Telethon asyncio loop."""
        return self.telegram.submit(coro).result(timeout=timeout)

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def url(self) -> str:
        return f"{self.origin}/?t={self.guard.token}"

    def start(self) -> None:
        httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        httpd.daemon_threads = True
        httpd.app = self          # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        kwargs={"poll_interval": 0.3},
                                        daemon=True, name="http")
        self._thread.start()
        LOG.info(f"listening on {self.origin}", module=MOD)

    def stop(self) -> None:
        self.bus.close()          # releases every SSE thread
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        LOG.info("http server stopped", module=MOD)


TOKEN_HEADER_NAME = TOKEN_HEADER
