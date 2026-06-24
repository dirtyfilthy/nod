"""Small built-in tools for the default agent wiring."""

from __future__ import annotations

from dataclasses import dataclass
import re
import subprocess
from pathlib import Path

import httpx

from nod.tools import Tool, tool

TEXT_LIMIT = 100_000
SHELL_TIMEOUT_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 30
READ_FILE_LINE_LIMIT = 1024
FILE_HEADER_RE = re.compile(r"^### FILE: (.+)$", re.MULTILINE)
SEARCH_MARKER = "<<<<<<< SEARCH"
DIVIDER_MARKER = "======="
REPLACE_MARKER = ">>>>>>> REPLACE"


@dataclass(frozen=True)
class _CUDOperation:
    path: str
    search: str | None = None
    replacement: str | None = None
    delete: bool = False
    error: str | None = None


def _truncate(text: str, limit: int = TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[truncated after {limit} characters]"


def read_file_tool() -> Tool:
    @tool(
        "read_file",
        "Read a paginated range of lines from a UTF-8 text file.",
        {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Path to the file to read.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Zero-indexed line offset to start reading from.",
                    "minimum": 0,
                },
                "line_count": {
                    "type": "integer",
                    "description": (
                        f"Number of lines to read, capped at {READ_FILE_LINE_LIMIT}."
                    ),
                    "minimum": 0,
                },
            },
            "required": ["filename", "start_line", "line_count"],
        },
    )
    def read_file(filename: str, start_line: int, line_count: int) -> str:
        if start_line < 0:
            return "error: start_line must be non-negative"
        if line_count < 0:
            return "error: line_count must be non-negative"

        try:
            data = Path(filename).expanduser().read_bytes()
        except OSError as exc:
            return f"error: could not read file '{filename}': {exc}"

        if b"\x00" in data:
            return f"error: file '{filename}' appears to be binary"

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return f"error: file '{filename}' appears to be binary"

        lines = text.splitlines()
        total_lines = len(lines)
        capped_count = min(line_count, READ_FILE_LINE_LIMIT)
        end_offset = min(start_line + capped_count, total_lines)
        selected = lines[start_line:end_offset]

        header = [
            f"start_offset: {start_line}",
            f"lines_returned: {len(selected)}",
            f"end_offset: {end_offset}",
            f"next_offset: {end_offset}",
            f"total_lines: {total_lines}",
        ]
        return "\n".join(header + [""] + selected)

    return read_file


def _parse_cud_blocks(edits: str) -> list[_CUDOperation]:
    matches = list(FILE_HEADER_RE.finditer(edits))
    if not matches:
        return [_CUDOperation(path="(input)", error="no ### FILE blocks found")]

    operations: list[_CUDOperation] = []
    if edits[: matches[0].start()].strip():
        operations.append(
            _CUDOperation(
                path="(input)", error="unexpected text before first ### FILE header"
            )
        )

    for i, match in enumerate(matches):
        path = match.group(1).strip()
        if not path:
            operations.append(_CUDOperation(path="(input)", error="empty file path"))
            continue

        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(edits)
        body = edits[match.end() : block_end]
        if body.startswith("\n"):
            body = body[1:]

        if body.strip() == "DELETE":
            operations.append(_CUDOperation(path=path, delete=True))
            continue

        operation = _parse_search_replace_block(path, body)
        operations.append(operation)

    return operations


def _parse_search_replace_block(path: str, body: str) -> _CUDOperation:
    search_start = f"{SEARCH_MARKER}\n"
    if not body.startswith(search_start):
        return _CUDOperation(
            path=path, error=f"expected {SEARCH_MARKER} or DELETE after file header"
        )

    rest = body[len(search_start) :]
    if rest.startswith(f"{DIVIDER_MARKER}\n"):
        search = ""
        replacement_part = rest[len(DIVIDER_MARKER) + 1 :]
    else:
        divider = f"\n{DIVIDER_MARKER}\n"
        divider_index = rest.find(divider)
        if divider_index == -1:
            return _CUDOperation(path=path, error=f"missing {DIVIDER_MARKER} divider")
        search = rest[:divider_index]
        replacement_part = rest[divider_index + len(divider) :]

    if replacement_part.startswith(REPLACE_MARKER):
        replacement = ""
        trailing = replacement_part[len(REPLACE_MARKER) :]
    else:
        end_marker = f"\n{REPLACE_MARKER}"
        end_index = replacement_part.find(end_marker)
        if end_index == -1:
            return _CUDOperation(
                path=path, error=f"missing {REPLACE_MARKER} end marker"
            )
        replacement = replacement_part[:end_index]
        trailing = replacement_part[end_index + len(end_marker) :]

    if trailing.strip():
        return _CUDOperation(
            path=path, error=f"unexpected text after {REPLACE_MARKER} end marker"
        )

    return _CUDOperation(path=path, search=search, replacement=replacement)


def _apply_cud_operation(operation: _CUDOperation) -> str:
    if operation.error is not None:
        return f"error: {operation.path}: {operation.error}"

    p = Path(operation.path).expanduser()
    if operation.delete:
        try:
            p.unlink()
        except FileNotFoundError:
            return f"error: {operation.path}: file does not exist"
        except OSError as exc:
            return f"error: {operation.path}: could not delete file: {exc}"
        return f"deleted {p}"

    if operation.search is None or operation.replacement is None:
        return f"error: {operation.path}: malformed operation"

    if operation.search == "":
        if p.exists():
            return f"error: {operation.path}: file already exists"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(operation.replacement, encoding="utf-8")
        except OSError as exc:
            return f"error: {operation.path}: could not create file: {exc}"
        return f"created {p}"

    try:
        content = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"error: {operation.path}: file does not exist"
    except OSError as exc:
        return f"error: {operation.path}: could not read file: {exc}"

    matches = content.count(operation.search)
    if matches == 0:
        return f"error: {operation.path}: SEARCH text not found"
    if matches > 1:
        return (
            f"error: {operation.path}: SEARCH text matched {matches} times; "
            "add more surrounding context to make it unique"
        )

    try:
        p.write_text(
            content.replace(operation.search, operation.replacement, 1),
            encoding="utf-8",
        )
    except OSError as exc:
        return f"error: {operation.path}: could not write file: {exc}"
    return f"updated {p}"


def cud_file_tool() -> Tool:
    @tool(
        "cud_file",
        "Create, update, or delete UTF-8 text files using SEARCH/REPLACE blocks.",
        {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "string",
                    "description": (
                        "One or more blocks. Update: ### FILE: path\n"
                        "<<<<<<< SEARCH\nold text\n=======\nnew text\n"
                        ">>>>>>> REPLACE. Create uses an empty SEARCH section. "
                        "Delete uses ### FILE: path followed by DELETE."
                    ),
                },
            },
            "required": ["edits"],
        },
    )
    def cud_file(edits: str) -> str:
        return "\n".join(_apply_cud_operation(op) for op in _parse_cud_blocks(edits))

    return cud_file


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
        cud_file_tool(),
        list_dir_tool(),
        run_shell_tool(),
        http_get_tool(),
    ]
