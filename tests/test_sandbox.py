from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from nod import toolhost
from nod.cli import _apply_sandbox_args, build_agent, main
from nod.config import Config, load
from nod.messages import ToolCall
from nod.sandbox import NO_NETWORK_NOTE, DockerSandbox, SandboxError, default_image, parse_mount
from nod.tools import ToolRegistry


class FakeDocker:
    """Records every docker command and answers from a script of results."""

    def __init__(self, *, image_exists: bool = True, stdout: str = "", returncode: int = 0):
        self.calls: list[tuple[list[str], dict]] = []
        self.image_exists = image_exists
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0 if self.image_exists else 1, "", "")
        if argv[:2] == ["docker", "exec"]:
            return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "boom")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def commands(self, prefix: list[str]) -> list[list[str]]:
        return [argv for argv, _ in self.calls if argv[: len(prefix)] == prefix]


def _sandbox(tmp_path, docker, **kwargs) -> DockerSandbox:
    return DockerSandbox(workspace=tmp_path, run=docker, **kwargs)


def test_run_argv_mounts_workspace_and_source_and_drops_privileges(tmp_path):
    argv = _sandbox(tmp_path, FakeDocker()).run_argv()

    assert f"{tmp_path.resolve()}:/workspace" in argv
    assert argv[argv.index("-w") + 1] == "/workspace"
    assert any(a.endswith(":/opt/nod-src/nod:ro") for a in argv)
    assert ["--cap-drop", "ALL"] == argv[argv.index("--cap-drop") : argv.index("--cap-drop") + 2]
    assert f"{os.getuid()}:{os.getgid()}" in argv
    assert "--network" not in argv
    assert argv[-2:] == ["sleep", "infinity"]


def test_run_argv_without_network_and_with_extra_mounts(tmp_path):
    argv = _sandbox(tmp_path, FakeDocker(), network=False, mounts=["/a:/b:ro"]).run_argv()

    assert argv[argv.index("--network") + 1] == "none"
    assert "/a:/b:ro" in argv


def test_start_builds_nods_image_only_when_missing(tmp_path):
    docker = FakeDocker(image_exists=False)
    sandbox = _sandbox(tmp_path, docker)
    sandbox.start()
    sandbox.start()

    (build,) = docker.commands(["docker", "build"])
    assert build[3] == default_image()
    assert len(docker.commands(["docker", "run"])) == 1
    sandbox.stop()
    assert docker.commands(["docker", "rm", "-f"])


def test_start_never_builds_a_custom_image(tmp_path):
    docker = FakeDocker(image_exists=False)
    _sandbox(tmp_path, docker, image="my-image").start()

    assert not docker.commands(["docker", "build"])
    assert docker.commands(["docker", "run"])[0][-3] == "my-image"


def test_start_reports_a_missing_docker(tmp_path):
    def no_docker(argv, **kwargs):
        raise FileNotFoundError("docker")

    with pytest.raises(SandboxError, match="docker not found"):
        _sandbox(tmp_path, no_docker).start()


def test_tool_call_runs_the_toolhost_in_the_container(tmp_path):
    docker = FakeDocker(stdout=json.dumps({"result": "exit code: 0\nhi"}))
    sandbox = _sandbox(tmp_path, docker)
    registry = ToolRegistry(sandbox.tools())

    result = registry.dispatch(ToolCall(id="1", name="run_shell", arguments={"command": "echo hi"}))

    assert result == "exit code: 0\nhi"
    (argv, kwargs) = [c for c in docker.calls if c[0][:2] == ["docker", "exec"]][0]
    assert argv[-3:] == ["/opt/toolhost/bin/python", "-m", "nod.toolhost"]
    assert sandbox.name in argv
    assert json.loads(kwargs["input"]) == {"name": "run_shell", "arguments": {"command": "echo hi"}}


def test_a_failed_container_call_reaches_the_model_as_an_error(tmp_path):
    sandbox = _sandbox(tmp_path, FakeDocker(stdout="", returncode=1))
    registry = ToolRegistry(sandbox.tools())

    result = registry.dispatch(ToolCall(id="1", name="list_dir", arguments={}))

    assert result.startswith("error: tool 'list_dir' failed: sandbox call failed (exit 1): boom")


def test_sandbox_offers_the_same_tools_as_the_host(tmp_path):
    from nod.builtins import builtin_tools

    host = [t.schema() for t in builtin_tools()]
    assert [t.schema() for t in _sandbox(tmp_path, FakeDocker()).tools()] == host


def test_job_kill_signals_the_group_in_the_container(tmp_path, monkeypatch):
    docker = FakeDocker()
    sandbox = _sandbox(tmp_path, docker)
    argv = sandbox.job_argv("sleep 100")
    assert argv[argv.index(sandbox.name) + 1 : argv.index(sandbox.name) + 3] == ["setsid", "-w"]

    killed = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    proc = subprocess.Popen(["true"])
    proc.wait()
    proc.args = argv

    sandbox.job_kill(proc, signal.SIGTERM)
    assert f"kill -{int(signal.SIGTERM)} -" in docker.commands(["docker", "exec"])[-1][-1]
    assert killed == []  # the host exec must outlive a SIGTERM

    sandbox.job_kill(proc, signal.SIGKILL)
    assert killed == [(proc.pid, signal.SIGKILL)]


def test_parse_mount(tmp_path):
    (tmp_path / "data").mkdir()
    data = str((tmp_path / "data").resolve())

    assert parse_mount("data", tmp_path) == f"{data}:{data}"
    assert parse_mount("data:ro", tmp_path) == f"{data}:{data}:ro"
    assert parse_mount(f"{data}:/mnt/d", tmp_path) == f"{data}:/mnt/d"
    assert parse_mount("data:/mnt/d:rw", tmp_path) == f"{data}:/mnt/d:rw"
    with pytest.raises(SandboxError, match="does not exist"):
        parse_mount("missing", tmp_path)
    with pytest.raises(SandboxError, match="absolute"):
        parse_mount("data:relative", tmp_path)


def test_toolhost_dispatches_one_call(tmp_path, monkeypatch):
    target = tmp_path / "f.txt"
    target.write_text("one\ntwo\n")
    request = {"name": "read_file", "arguments": {"filename": str(target), "start_line": 1, "line_count": 5}}
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    monkeypatch.setattr(sys, "stdout", out)

    assert toolhost.main() == 0
    assert json.loads(out.getvalue())["result"].endswith("\ntwo")


def test_toolhost_reports_unknown_tools(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"name": "nope", "arguments": {}}'))
    monkeypatch.setattr(sys, "stdout", out)

    toolhost.main()
    assert json.loads(out.getvalue())["result"] == "error: no such tool 'nope'"


def test_config_reads_the_sandbox_table(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[sandbox]\nenabled = true\nworkspace = "work"\nmounts = ["/data:ro"]\nnetwork = false\n'
    )
    cfg = load(path)

    assert cfg.sandbox and not cfg.sandbox_network
    assert cfg.sandbox_mounts == ["/data:ro"]
    assert cfg.resolve(cfg.sandbox_workspace) == tmp_path / "work"
    assert Config().sandbox is False


def test_any_sandbox_flag_turns_it_on(tmp_path, monkeypatch):
    import argparse

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    cfg = Config()
    args = argparse.Namespace(sandbox=False, workspace=None, mount=["data:ro"], no_network=True)
    _apply_sandbox_args(cfg, args)

    data = (tmp_path / "data").resolve()
    assert cfg.sandbox and not cfg.sandbox_network
    assert cfg.sandbox_mounts == [f"{data}:{data}:ro"]


def test_bad_mount_on_the_command_line_exits_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["--mount", "missing", "chat"]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_build_agent_routes_builtin_tools_through_the_sandbox(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(DockerSandbox, "start", lambda self: None)
    monkeypatch.setattr(DockerSandbox, "call", lambda self, name, args: calls.append(name) or "ok")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")

    cfg = Config(memory_dir=str(tmp_path / "m"), skills_dirs=[], sandbox=True, sandbox_workspace=str(tmp_path))
    agent = build_agent(cfg)

    assert agent.registry.dispatch(ToolCall(id="1", name="list_dir", arguments={})) == "ok"
    assert calls == ["list_dir"]
    assert NO_NETWORK_NOTE not in agent.system_prompt()


def test_build_agent_tells_the_model_when_the_network_is_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(DockerSandbox, "start", lambda self: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    (tmp_path / "SOUL.md").write_text("I am Nod.\n")

    cfg = Config(
        memory_dir=str(tmp_path / "m"),
        skills_dirs=[],
        soul_path=str(tmp_path / "SOUL.md"),
        sandbox=True,
        sandbox_workspace=str(tmp_path),
        sandbox_network=False,
    )
    prompt = build_agent(cfg).system_prompt()

    assert prompt.startswith("I am Nod.\n\n" + NO_NETWORK_NOTE)


# -- against a real Docker daemon: NOD_DOCKER_TESTS=1 to run -----------------

docker_only = pytest.mark.skipif(
    os.environ.get("NOD_DOCKER_TESTS") != "1", reason="set NOD_DOCKER_TESTS=1 to run"
)


@docker_only
def test_real_sandbox_end_to_end(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "note.txt").write_text("from the host\n")

    sandbox = DockerSandbox(workspace=workspace, mounts=[f"{shared}:/mnt/shared:ro"], network=False)
    registry = ToolRegistry(sandbox.tools())

    def run(name, **arguments):
        return registry.dispatch(ToolCall(id="1", name=name, arguments=arguments))

    try:
        created = run("cud_file", edits="### FILE: hello.txt\n<<<<<<< SEARCH\n=======\nhi\n>>>>>>> REPLACE")
        assert created == "created hello.txt"
        assert (workspace / "hello.txt").read_text() == "hi"
        assert "hi" in run("read_file", filename="/workspace/hello.txt", start_line=0, line_count=5)
        assert "note.txt" in run("list_dir", path="/mnt/shared")
        assert "Read-only" in run("run_shell", command="touch /mnt/shared/x")
        assert f"\n{os.getuid()}" in run("run_shell", command="id -u")
        assert run("run_shell", command=f"test -e {Path.home()}; echo $?").endswith("\n1\n")
        assert run("http_get", url="https://example.com").startswith("error:")

        # A background job runs in the container and its group dies on SIGTERM.
        job = subprocess.Popen(sandbox.job_argv("sleep 300"), start_new_session=True)
        for _ in range(50):
            if "sleep 300" in run("run_shell", command="ps -eo args"):
                break
        sandbox.job_kill(job, signal.SIGTERM)
        assert job.wait(timeout=20) != 0
        assert "sleep 300" not in run("run_shell", command="ps -eo args")
    finally:
        sandbox.stop()
