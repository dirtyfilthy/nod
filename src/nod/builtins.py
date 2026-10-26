"""Small built-in tools for the default agent wiring."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
import subprocess
import tempfile
import threading
from pathlib import Path

import httpx

from nod.tools import Tool, tool

TEXT_LIMIT = 100_000
SHELL_TIMEOUT_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 30
READ_FILE_LINE_LIMIT = 1024
SHELL_MAX_LINES = 2000
SHELL_MAX_BYTES = 50 * 1024  # 50 KB
FILE_HEADER_RE = re.compile(r"^### FILE: (.+)$", re.MULTILINE)
# OpenAI's apply_patch format, which models sometimes send to cud_file.
APPLY_PATCH_RE = re.compile(
    r"^\*\*\* (Begin Patch|Update File:|Add File:|Delete File:)", re.MULTILINE
)
# One cud_file call at a time per process, so agents running in threads can't
# lose each other's edits between a read and a write. Shell edits aren't
# covered.
_CUD_LOCK = threading.Lock()


def _truncate_shell_output(
    output: str, output_dir: str | None = None
) -> tuple[str, str | None]:
    """Return (text_for_model, tmp_path_or_none).

    Keeps the last SHELL_MAX_LINES lines or SHELL_MAX_BYTES bytes, whichever
    limit is hit first.  If the output is truncated the full text is written to
    a temp file (in ``output_dir`` if given) and the path is appended so the
    model can read it.
    """
    lines = output.splitlines()
    total_lines = len(lines)
    total_bytes = len(output.encode())

    if total_lines <= SHELL_MAX_LINES and total_bytes <= SHELL_MAX_BYTES:
        return output, None

    # Walk backwards collecting lines until we hit a limit.
    kept: list[str] = []
    kept_bytes = 0
    for line in reversed(lines):
        line_bytes = len(line.encode()) + 1  # +1 for newline
        if len(kept) >= SHELL_MAX_LINES or kept_bytes + line_bytes > SHELL_MAX_BYTES:
            break
        kept.append(line)
        kept_bytes += line_bytes
    kept.reverse()

    # Write full output to a temp file the model can read_file later.
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".log",
        prefix="nod-shell-",
        dir=output_dir,
        delete=False,
        encoding="utf-8",
    )
    tmp.write(output)
    tmp.close()

    skipped = total_lines - len(kept)
    tail = "\n".join(kept)
    tail += (
        f"\n\n[Showing last {len(kept)} of {total_lines} lines. "
        f"Full output: {tmp.name}]"
    )
    return tail, tmp.name


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
    def read_file(
        filename: str,
        start_line: int | None = None,
        line_count: int | None = None,
        offset: int | None = None,
        limit: int | None = None,
    ) -> str:
        # offset/limit are accepted as aliases because models often reach for
        # those names; they aren't advertised in the schema.
        if start_line is None:
            start_line = offset if offset is not None else 0
        if line_count is None:
            line_count = limit
        if line_count is None:
            return "error: line_count is required"
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
    if not matches and APPLY_PATCH_RE.search(edits):
        return [
            _CUDOperation(
                path="(input)",
                error=(
                    "this looks like *** Begin Patch / *** Update File: format, "
                    "which cud_file does not accept. Use one block per change:\n"
                    "### FILE: path/to/file.py\n"
                    f"{SEARCH_MARKER}\nold text\n{DIVIDER_MARKER}\nnew text\n"
                    f"{REPLACE_MARKER}\n"
                    "(empty SEARCH section to create a file; DELETE instead of "
                    "the markers to delete one)"
                ),
            )
        ]
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
        return (
            f"error: {operation.path}: SEARCH text not found; the file may have "
            "changed, so re-read it and copy SEARCH from its current contents"
        )
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
        operations = _parse_cud_blocks(edits)
        with _CUD_LOCK:
            return "\n".join(_apply_cud_operation(op) for op in operations)

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


def run_shell_tool(
    argv: Callable[[str], list[str]] | None = None,
    timeout: int = SHELL_TIMEOUT_SECONDS,
    output_dir: str | None = None,
) -> Tool:
    """Run commands through the local shell, or through ``argv(command)`` if given.

    ``argv`` lets a caller route commands somewhere else (e.g. ``docker exec``)
    while keeping the same timeout, output merging, and truncation.
    ``output_dir`` is where the full text of truncated output is saved (default:
    the system temp dir); a caller that routes commands elsewhere can point it
    at a directory the commands can also see.
    """

    @tool(
        "run_shell",
        "Run a shell command and return its interleaved stdout/stderr and the exit code.",
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
                argv(command) if argv else command,
                shell=argv is None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            # The output captured before the kill is often what the model needs
            # (e.g. which test was running). It comes back as bytes even in text
            # mode.
            partial = exc.output or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", errors="replace")
            truncated, _ = _truncate_shell_output(partial or "(no output)", output_dir)
            return (
                f"error: command timed out after {timeout} seconds and was killed. "
                f"Output before the timeout:\n{truncated}"
            )

        output = completed.stdout
        if not output:
            output = "(no output)"
        truncated, _ = _truncate_shell_output(output, output_dir)
        return f"exit code: {completed.returncode}\n{truncated}"

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


def builtin_tools(
    shell_argv: Callable[[str], list[str]] | None = None,
    shell_timeout: int = SHELL_TIMEOUT_SECONDS,
    shell_output_dir: str | None = None,
) -> list[Tool]:
    return [
        read_file_tool(),
        cud_file_tool(),
        list_dir_tool(),
        run_shell_tool(shell_argv, timeout=shell_timeout, output_dir=shell_output_dir),
        http_get_tool(),
    ]
