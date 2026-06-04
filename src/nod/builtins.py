"""Small built-in tools for the default agent wiring."""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx

from nod.tools import Tool, tool

TEXT_LIMIT = 100_000
SHELL_TIMEOUT_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 30


def _truncate(text: str, limit: int = TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[truncated after {limit} characters]"


def read_file_tool() -> Tool:
    @tool(
        "read_file",
        "Read a UTF-8 text file from disk.",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read.",
                }
            },
            "required": ["path"],
        },
    )
    def read_file(path: str) -> str:
        try:
            return _truncate(Path(path).expanduser().read_text(encoding="utf-8"))
        except OSError as exc:
            return f"error: could not read file '{path}': {exc}"

    return read_file


def write_file_tool() -> Tool:
    @tool(
        "write_file",
        "Write UTF-8 text to disk, creating parent directories as needed.",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write.",
                },
                "content": {
                    "type": "string",
                    "description": "Text content to write.",
                },
            },
            "required": ["path", "content"],
        },
    )
    def write_file(path: str, content: str) -> str:
        p = Path(path).expanduser()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"error: could not write file '{path}': {exc}"
        return f"wrote {len(content.encode('utf-8'))} bytes to {p}"

    return write_file


def list_dir_tool() -> Tool:
    @tool(
        "list_dir",
        "List one directory non-recursively.",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to list. Defaults to the current directory.",
                }
            },
        },
    )
    def list_dir(path: str = ".") -> str:
        p = Path(path).expanduser()
        try:
            entries = sorted(
                child.name + ("/" if child.is_dir() else "") for child in p.iterdir()
            )
        except OSError as exc:
            return f"error: could not list directory '{path}': {exc}"
        return "\n".join(entries)

    return list_dir


def run_shell_tool() -> Tool:
    @tool(
        "run_shell",
        "Run a shell command and return stdout, stderr, and the exit code.",
        {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute.",
                }
            },
            "required": ["command"],
        },
    )
    def run_shell(command: str) -> str:
        try:
            completed = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=SHELL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return f"error: command timed out after {SHELL_TIMEOUT_SECONDS} seconds"

        output = completed.stdout + completed.stderr
        if not output:
            output = "(no output)"
        return f"exit code: {completed.returncode}\n{output}"

    return run_shell


def http_get_tool() -> Tool:
    @tool(
        "http_get",
        "Fetch a URL with HTTP GET and return the response body.",
        {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to fetch.",
                }
            },
            "required": ["url"],
        },
    )
    def http_get(url: str) -> str:
        try:
            response = httpx.get(
                url, timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True
            )
        except httpx.HTTPError as exc:
            return f"error: could not fetch '{url}': {exc}"

        reason = getattr(response, "reason_phrase", "")
        status = f"{response.status_code} {reason}".strip()
        return f"status: {status}\n{_truncate(response.text)}"

    return http_get


def builtin_tools() -> list[Tool]:
    return [
        read_file_tool(),
        write_file_tool(),
        list_dir_tool(),
        run_shell_tool(),
        http_get_tool(),
    ]
