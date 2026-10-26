from __future__ import annotations

import json
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.client import HTTPConnection

from conftest import FakeProvider

from nod.agent import Agent
from nod.memory import InMemoryMemory
from nod.messages import assistant
from nod.server import build_server

import pytest

TOKEN = "secret-token"


@contextmanager
def running(agent: Agent):
    # Port 0 -> the OS hands us a free ephemeral port.
    server = build_server(agent, TOKEN, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield host, port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(host, port, method, path, *, body=None, token=TOKEN):
    conn = HTTPConnection(host, port, timeout=5)
    headers = {}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    conn.close()
    parsed = json.loads(raw) if raw else None
    return resp.status, parsed


def _agent(replies):
    return Agent(provider=FakeProvider(replies), memory=InMemoryMemory())


def test_build_server_requires_token():
    with pytest.raises(ValueError):
        build_server(_agent([]), "")


def test_healthz_needs_no_auth():
    with running(_agent([assistant("hi")])) as (host, port):
        status, body = request(host, port, "GET", "/healthz", token=None)
    assert status == 200
    assert body == {"status": "ok"}


def test_chat_happy_path():
    with running(_agent([assistant("hello there")])) as (host, port):
        status, body = request(host, port, "POST", "/chat", body={"message": "hi"})
    assert status == 200
    assert body == {"session": "api", "reply": "hello there"}


def test_chat_rejects_missing_token():
    with running(_agent([assistant("x")])) as (host, port):
        status, _ = request(host, port, "POST", "/chat", body={"message": "hi"}, token=None)
    assert status == 401


def test_chat_rejects_wrong_token():
    with running(_agent([assistant("x")])) as (host, port):
        status, _ = request(host, port, "POST", "/chat", body={"message": "hi"}, token="nope")
    assert status == 401


def test_chat_rejects_empty_message():
    with running(_agent([assistant("x")])) as (host, port):
        status, _ = request(host, port, "POST", "/chat", body={"message": "   "})
    assert status == 422


def test_chat_rejects_non_json():
    with running(_agent([assistant("x")])) as (host, port):
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            "/chat",
            body=b"not json",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )
        status = conn.getresponse().status
        conn.close()
    assert status == 400


def test_sessions_are_isolated():
    agent = _agent([assistant("a1"), assistant("b1")])
    with running(agent) as (host, port):
        _, b1 = request(host, port, "POST", "/chat", body={"message": "x", "session": "a"})
        _, b2 = request(host, port, "POST", "/chat", body={"message": "y", "session": "b"})
    assert b1 == {"session": "a", "reply": "a1"}
    assert b2 == {"session": "b", "reply": "b1"}


def test_provider_error_becomes_502():
    def boom(_messages):
        raise RuntimeError("brain offline")

    with running(_agent([boom])) as (host, port):
        status, body = request(host, port, "POST", "/chat", body={"message": "hi"})
    assert status == 502
    assert "brain offline" in body["detail"]


def test_unknown_route_is_404():
    with running(_agent([assistant("x")])) as (host, port):
        status, _ = request(host, port, "GET", "/nope", token=None)
    assert status == 404


def test_access_log_line(capsys):
    with running(_agent([assistant("hi")])) as (host, port):
        request(host, port, "POST", "/chat", body={"message": "hi", "session": "s9"})
        # The handler thread flushes its log line just after the response body, so
        # poll briefly rather than racing it.
        deadline = time.time() + 2
        out = ""
        while time.time() < deadline:
            out += capsys.readouterr().out
            if "POST /chat 200" in out:
                break
            time.sleep(0.02)
    assert "POST /chat 200" in out
    assert "session=s9" in out


# A subprocess so we can deliver a real SIGTERM (as systemd would) and assert the
# server exits cleanly instead of being hard-killed mid-loop.
_SERVE_SCRIPT = """
from nod.agent import Agent
from nod.memory import InMemoryMemory
from nod.messages import Message
from nod.server import HTTPGateway

class StubProvider:
    def complete(self, messages, tools=None, on_token=None):
        return Message(role="assistant", content="ok")

HTTPGateway(host="127.0.0.1", port=0, token="t").run(
    Agent(provider=StubProvider(), memory=InMemoryMemory())
)
"""


def test_graceful_sigterm_shutdown():
    proc = subprocess.Popen(
        [sys.executable, "-c", _SERVE_SCRIPT],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 10
        line = ""
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line or "serving" in line:
                break
        assert "serving" in line, f"server did not start (got {line!r})"

        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
        assert proc.returncode == 0, f"unclean exit {proc.returncode}: {out!r}"
        assert "Nod stopped." in out
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_wakeup_reply_is_attached_to_next_response():
    # An inbox post between requests wakes the agent on its own thread; the
    # reply of that turn rides along with the next response for the session.
    agent = _agent([assistant("first"), assistant("woke"), assistant("second")])
    with running(agent) as (host, port):
        _, b1 = request(host, port, "POST", "/chat", body={"message": "x", "session": "s"})
        agent.inbox.post("s", "<job-finished id=1>x</job-finished>")
        deadline = time.time() + 5
        while time.time() < deadline and len(agent.provider.calls) < 2:
            time.sleep(0.02)
        # Let the wake thread store its reply before the next request collects.
        deadline = time.time() + 5
        while time.time() < deadline and agent.memory.load("s")[-1].content != "woke":
            time.sleep(0.02)
        time.sleep(0.05)
        _, b2 = request(host, port, "POST", "/chat", body={"message": "y", "session": "s"})
        _, b3 = request(host, port, "POST", "/chat", body={"message": "z", "session": "s"})
    assert b1 == {"session": "s", "reply": "first"}
    assert b2 == {"session": "s", "reply": "second", "wakeups": ["woke"]}
    assert "wakeups" not in b3


def test_concurrent_requests_on_one_session_run_one_at_a_time():
    import threading as _threading

    order: list[str] = []

    def slow(_messages):
        order.append("start")
        time.sleep(0.2)
        order.append("end")
        return assistant("ok")

    agent = _agent([slow, slow])
    with running(agent) as (host, port):
        threads = [
            _threading.Thread(
                target=request, args=(host, port, "POST", "/chat"),
                kwargs={"body": {"message": "x", "session": "same"}},
            )
            for _ in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
    assert order == ["start", "end", "start", "end"]
