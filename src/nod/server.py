"""HTTP gateway: run the agent as a service, on the standard library alone.

No web framework. A `ThreadingHTTPServer` maps cleanly onto the synchronous
agent loop -- each request gets its own thread, and a per-session lock keeps
concurrent calls to the same conversation from racing on the `Memory` store.

The API is two endpoints:

    POST /chat     {"message": "...", "session": "..."} -> {"session", "reply"}
    GET  /healthz  -> {"status": "ok"}   (no auth, no model call)

`POST /chat` requires `Authorization: Bearer <token>`; the built-in tools have
the reach of the process user, so an open port would be a liability.
"""

from __future__ import annotations

import hmac
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nod.agent import Agent

# Cap request bodies so a bad/hostile caller can't make us read forever.
MAX_BODY_BYTES = 1 << 20  # 1 MiB


class _SessionLocks:
    """Hands out one lock per session id, created on first use."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def get(self, session_id: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[session_id] = lock
            return lock


def build_server(
    agent: Agent,
    token: str,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    default_session: str = "api",
) -> ThreadingHTTPServer:
    """Build (but don't start) the threaded HTTP server around a wired agent.

    `token` is required: an unauthenticated endpoint would expose run_shell and
    friends to anyone who can reach the port.
    """

    if not token:
        raise ValueError("an API token is required; set $NOD_API_TOKEN before serving")

    locks = _SessionLocks()

    class Handler(BaseHTTPRequestHandler):
        server_version = "nod/0.1"
        protocol_version = "HTTP/1.1"

        _started: float | None = None
        _session: str | None = None

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self._access_log(status)

        def _access_log(self, status: int) -> None:
            # One concise line per request to stdout for journald. Never logs the
            # token or message body -- only routing metadata.
            took = ""
            if self._started is not None:
                took = f" {(time.monotonic() - self._started) * 1000:.0f}ms"
            session = f" session={self._session}" if self._session else ""
            sys.stdout.write(
                f"{self.address_string()} {self.command} {self.path} "
                f"{status}{session}{took}\n"
            )
            sys.stdout.flush()

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            prefix = "Bearer "
            supplied = header[len(prefix) :] if header.startswith(prefix) else ""
            return bool(supplied and hmac.compare_digest(supplied, token))

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            self._started = time.monotonic()
            if self.path == "/healthz":
                self._send_json(200, {"status": "ok"})
            else:
                self._send_json(404, {"detail": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            self._started = time.monotonic()
            if self.path != "/chat":
                self._send_json(404, {"detail": "not found"})
                return
            if not self._authorized():
                self._send_json(401, {"detail": "invalid or missing token"})
                return

            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send_json(400, {"detail": "missing or oversized body"})
                return
            try:
                data = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                self._send_json(400, {"detail": "body must be valid JSON"})
                return
            if not isinstance(data, dict):
                self._send_json(400, {"detail": "body must be a JSON object"})
                return

            message = data.get("message")
            if not isinstance(message, str) or not message.strip():
                self._send_json(422, {"detail": "message must be a non-empty string"})
                return
            session = data.get("session") or default_session
            if not isinstance(session, str):
                self._send_json(422, {"detail": "session must be a string"})
                return
            self._session = session

            try:
                with locks.get(session):
                    reply = agent.send(message, session_id=session)
            except Exception as exc:  # noqa: BLE001 - report, keep serving
                self._send_json(502, {"detail": str(exc)})
                return
            self._send_json(200, {"session": session, "reply": reply})

        def log_message(self, *args) -> None:
            # Silence per-request stderr logging; the startup banner is enough.
            pass

    return ThreadingHTTPServer((host, port), Handler)


class HTTPGateway:
    """Serve a wired `Agent` over HTTP using only the standard library."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        token: str,
        default_session: str = "api",
    ) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.default_session = default_session

    def run(self, agent: Agent) -> None:
        server = build_server(
            agent,
            self.token,
            host=self.host,
            port=self.port,
            default_session=self.default_session,
        )
        host, port = server.server_address[:2]
        print(
            f"Nod serving on http://{host}:{port}  (SIGTERM/Ctrl-C to stop)",
            flush=True,
        )

        # serve_forever() blocks, and a signal handler can't call shutdown()
        # from inside it without deadlocking. So serve on a worker thread and let
        # the main thread wait for a stop signal, then shut down cleanly. Signals
        # can only be registered on the main thread; off it (e.g. tests) we fall
        # back to a plain blocking serve.
        if threading.current_thread() is threading.main_thread():
            stop = threading.Event()
            previous = {
                sig: signal.signal(sig, lambda *_: stop.set())
                for sig in (signal.SIGTERM, signal.SIGINT)
            }
            worker = threading.Thread(target=server.serve_forever, name="nod-http")
            worker.start()
            try:
                stop.wait()
            finally:
                server.shutdown()
                worker.join()
                server.server_close()
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
            print("Nod stopped.", flush=True)
        else:
            try:
                server.serve_forever()
            finally:
                server.server_close()
