"""Run the built-in tools inside a Docker container instead of on the host.

Nod itself (the loop, the API key, memory, config) stays on the host. One
container is started per Nod process and every built-in tool call is run in it
with `docker exec ... python -m nod.toolhost`, using the host's own copy of the
tool code, mounted read-only. So the tools behave exactly as they do on the
host, but they see only what is mounted:

- the workspace, at /workspace, which is also the tools' working directory;
- any extra mounts, at the same path as on the host unless one is given;
- Nod's source, read-only, so the toolhost can import it.

With `network=False` the container gets no network at all, so `http_get` and
anything the shell tries to fetch fail. The provider is unaffected: it runs on
the host.

The container runs as the host user (files it writes in a mount stay yours),
with every capability dropped, no privilege gain, and memory and process
limits. It is removed when Nod exits.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nod import builtins as builtins_module
from nod.tools import Tool

WORKSPACE = "/workspace"
NOD_SRC = "/opt/nod-src"
TOOLHOST_PYTHON = "/opt/toolhost/bin/python"
DOCKERFILE = Path(__file__).with_name("sandbox.Dockerfile")
MEMORY_LIMIT = "2g"
PIDS_LIMIT = 512
# The tools enforce their own timeouts inside the container; this one only
# catches a `docker exec` that never returns.
CALL_TIMEOUT_SECONDS = builtins_module.SHELL_TIMEOUT_SECONDS + 30
JOB_PID_DIR = "/tmp/nod-jobs"

Runner = Callable[..., subprocess.CompletedProcess]


class SandboxError(RuntimeError):
    """The sandbox couldn't be set up (no docker, bad mount, failed start)."""


def default_image() -> str:
    """Tag for Nod's own image: a new Dockerfile means a new tag and a rebuild."""

    digest = hashlib.sha256(DOCKERFILE.read_bytes()).hexdigest()[:12]
    return f"nod-sandbox:{digest}"


def parse_mount(spec: str, base: Path) -> str:
    """Turn `HOST[:CONTAINER][:ro|rw]` into a `docker -v` value.

    A relative HOST resolves against `base`; with no CONTAINER path the mount
    appears at the same path as on the host.
    """

    parts = spec.split(":")
    mode = ""
    if len(parts) > 1 and parts[-1] in ("ro", "rw"):
        mode = parts.pop()
    if not parts[0] or len(parts) > 2:
        raise SandboxError(f"bad mount '{spec}': expected HOST[:CONTAINER][:ro]")
    host = (base / Path(parts[0]).expanduser()).resolve()
    if not host.exists():
        raise SandboxError(f"bad mount '{spec}': {host} does not exist")
    container = parts[1] if len(parts) == 2 else str(host)
    if not container.startswith("/"):
        raise SandboxError(f"bad mount '{spec}': container path must be absolute")
    return f"{host}:{container}" + (f":{mode}" if mode else "")


def _run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


class DockerSandbox:
    def __init__(
        self,
        *,
        workspace: str | Path,
        mounts: list[str] | None = None,
        network: bool = True,
        image: str = "",
        run: Runner = _run,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.mounts = list(mounts or [])
        self.network = network
        self.image = image or default_image()
        self.name = f"nod-sandbox-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._run = run
        self._lock = threading.Lock()
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Build Nod's image if needed and start the container. Idempotent."""

        with self._lock:
            if self._started:
                return
            if not self.workspace.is_dir():
                raise SandboxError(f"workspace {self.workspace} is not a directory")
            try:
                if self.image == default_image() and not self._image_exists():
                    self._build_image()
                result = self._run(self.run_argv())
            except FileNotFoundError:
                raise SandboxError("docker not found on PATH") from None
            if result.returncode != 0:
                raise SandboxError(f"could not start the sandbox: {result.stderr.strip()}")
            self._started = True
            atexit.register(self.stop)

    def stop(self) -> None:
        with self._lock:
            if self._started:
                self._run(["docker", "rm", "-f", self.name])
                self._started = False

    def run_argv(self) -> list[str]:
        """The `docker run` that starts the container."""

        nod_src = Path(builtins_module.__file__).resolve().parent
        argv = [
            "docker", "run", "-d", "--rm", "--name", self.name,
            "--label", "nod.sandbox=1", "--init",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", MEMORY_LIMIT, "--pids-limit", str(PIDS_LIMIT),
            "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
            "-v", f"{self.workspace}:{WORKSPACE}", "-w", WORKSPACE,
            "-v", f"{nod_src}:{NOD_SRC}/nod:ro",
        ]
        if not self.network:
            argv += ["--network", "none"]
        for mount in self.mounts:
            argv += ["-v", mount]
        return argv + [self.image, "sleep", "infinity"]

    def _image_exists(self) -> bool:
        return self._run(["docker", "image", "inspect", self.image]).returncode == 0

    def _build_image(self) -> None:
        print(f"[sandbox] building {self.image} (first run only)...", flush=True)
        result = self._run(
            ["docker", "build", "-t", self.image, "-"],
            input=DOCKERFILE.read_text(encoding="utf-8"),
        )
        if result.returncode != 0:
            raise SandboxError(f"could not build {self.image}: {result.stderr.strip()[-2000:]}")

    # -- tools -------------------------------------------------------------

    def tools(self) -> list[Tool]:
        """The built-in tools, each running inside the container."""

        return [self._wrap(t) for t in builtins_module.builtin_tools()]

    def _wrap(self, t: Tool) -> Tool:
        # cud_file holds a process-wide lock on the host so threads can't lose
        # each other's edits; each call here is its own process, so keep
        # holding it on this side.
        lock = builtins_module._CUD_LOCK if t.name == "cud_file" else contextlib.nullcontext()

        def call(**arguments: Any) -> str:
            with lock:
                return self.call(t.name, arguments)

        return Tool(name=t.name, description=t.description, parameters=t.parameters, func=call)

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Run one tool call in the container and return its result."""

        self.start()
        argv = [
            "docker", "exec", "-i",
            "-e", f"PYTHONPATH={NOD_SRC}", "-e", "PYTHONDONTWRITEBYTECODE=1",
            self.name, TOOLHOST_PYTHON, "-m", "nod.toolhost",
        ]
        request = json.dumps({"name": name, "arguments": arguments})
        try:
            result = self._run(argv, input=request, timeout=CALL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"no answer from the sandbox after {CALL_TIMEOUT_SECONDS} s")
        try:
            return json.loads(result.stdout)["result"]
        except (ValueError, KeyError, TypeError):
            detail = (result.stderr or result.stdout).strip()[-2000:]
            raise RuntimeError(f"sandbox call failed (exit {result.returncode}): {detail}")

    # -- background shell jobs (nod-extras) --------------------------------

    def job_argv(self, command: str) -> list[str]:
        """Run a background job in the container, in a process group of its own.

        The group's id goes in a file in the container so `job_kill` can reach
        it: killing the host's `docker exec` alone would leave it running.
        """

        self.start()
        pid_file = f"{JOB_PID_DIR}/{uuid.uuid4().hex}"
        return [
            "docker", "exec", self.name,
            # -w keeps `docker exec` attached until the command ends.
            "setsid", "-w", "sh", "-c",
            'mkdir -p "${2%/*}" && echo $$ > "$2" && eval "$1"',
            "sh", command, pid_file,
        ]

    def job_kill(self, proc: subprocess.Popen, sig: int) -> None:
        """Signal a job's process group inside the container.

        The host `docker exec` is killed too, but only when the group can't be
        reached or the signal is SIGKILL: on a SIGTERM it has to stay up until
        the command really exits.
        """

        pid_file = proc.args[-1]
        result = self._run(
            ["docker", "exec", self.name, "sh", "-c", f'kill -{int(sig)} -"$(cat {pid_file})"'],
            timeout=30,
        )
        if result.returncode != 0 or sig == signal.SIGKILL:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, sig)
