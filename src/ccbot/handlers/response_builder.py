"""Response message building for Telegram delivery.

Builds paginated response messages from Claude Code output:
  - Handles different content types (text, thinking, tool_use, tool_result)
  - Splits long messages into pages within Telegram's 4096 char limit
  - Truncates thinking content to keep messages compact

Markdown conversion is NOT done here — the send layer (message_sender,
message_queue) handles convert_markdown() so each message is converted
exactly once.

Key functions:
  - build_response_parts: Build paginated response messages
  - build_commentary_parts: Build lossless commentary pages without 3000-char clipping
"""

import json
import re

from ..markdown_v2 import convert_markdown_tables
from ..telegram_sender import split_message
from ..transcript_parser import TranscriptParser

_CONTENT_PREFIXES: dict[str, str] = {
    "thinking": "∴ Thinking…",
    "commentary": "ℹ Commentary",
    "reasoning": "∴ Reasoning…",
    "command_execution": "⌘ Command",
    "tool_use": "🛠 Tool",
    "tool_progress": "↻ Tool Progress",
    "tool_result": "↳ Tool Output",
    "file_change": "Δ Files",
}

_FUNCTION_CALL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\((.*)\)\s*$", re.DOTALL)
_FILE_CHANGE_STATUSES = {"applied", "pending", "completed", "failed"}
_FENCED_BLOCK_RE = re.compile(r"```[A-Za-z0-9_-]*\n[\s\S]*?\n```")
_PREVIEW_FOOTER_RE = re.compile(r"^preview\s+\d+/\d+\s+lines$", re.IGNORECASE)
_REDUNDANT_OUTPUT_FOOTER_RE = re.compile(
    r"^(?:[a-z ]*·\s*)?output\s+\d+\s+line\(s\)$",
    re.IGNORECASE,
)


def _clip_code_lines(
    lines: list[str],
    *,
    max_lines: int = 20,
    max_chars: int = 180,
) -> tuple[list[str], int]:
    clipped = [
        line if len(line) <= max_chars else line[: max_chars - 1].rstrip() + "…"
        for line in lines[:max_lines]
    ]
    return clipped, len(lines)


def _split_shell_chain_line(line: str) -> list[str]:
    """Split top-level shell chains so command previews show useful rows."""
    segments: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if escaped:
            current.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            index += 1
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            current.append(char)
            quote = char
            index += 1
            continue
        if line.startswith("&&", index) or char == ";":
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
            index += 2 if line.startswith("&&", index) else 1
            while index < len(line) and line[index].isspace():
                index += 1
            continue
        current.append(char)
        index += 1
    segment = "".join(current).strip()
    if segment:
        segments.append(segment)
    return segments or [line.strip()]


def _shell_preview_lines(text: str) -> list[str]:
    lines = [line.rstrip() for line in text.strip().splitlines() if line.strip()]
    expanded: list[str] = []
    for line in lines:
        expanded.extend(_split_shell_chain_line(line))
    return expanded


def _preview_footer(total_lines: int, shown_lines: int) -> str:
    remaining = max(0, total_lines - shown_lines)
    if remaining <= 0:
        return ""
    return f"preview {shown_lines}/{total_lines} lines"


def _format_json_code_block(
    value: str | dict | list,
    *,
    max_lines: int = 20,
    max_chars: int = 180,
) -> str | None:
    try:
        if isinstance(value, str):
            parsed = json.loads(value)
        else:
            parsed = value
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    pretty = json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True)
    lines = pretty.splitlines()
    clipped, total_lines = _clip_code_lines(
        lines,
        max_lines=max_lines,
        max_chars=max_chars,
    )
    block = "```json\n" + "\n".join(clipped) + "\n```"
    footer = _preview_footer(total_lines, len(clipped))
    return "\n\n".join(part for part in (block, footer) if part)


def _format_function_call_json(text: str) -> str | None:
    match = _FUNCTION_CALL_RE.match(text.strip())
    if not match:
        return None
    name, payload = match.groups()
    block = _format_json_code_block(payload)
    if not block:
        return None
    return f"{name}\n{block}"


def _format_file_change_block(text: str) -> str:
    stripped = text.strip()
    if not stripped or stripped.startswith("```"):
        return stripped
    lines = [line.rstrip() for line in stripped.splitlines() if line.strip()]
    if len(lines) <= 1:
        return stripped
    first = lines[0].strip().lower()
    if first in _FILE_CHANGE_STATUSES and len(lines) > 1:
        clipped, total_lines = _clip_code_lines(
            lines[1:],
            max_lines=12,
            max_chars=140,
        )
        body = "\n".join(clipped)
        footer = _preview_footer(total_lines, len(clipped))
        result = [lines[0], f"```sh\n{body}\n```"]
        if footer:
            result.append("")
            result.append(footer)
        return "\n".join(result)
    clipped, total_lines = _clip_code_lines(lines, max_lines=12, max_chars=140)
    body = "\n".join(clipped)
    footer = _preview_footer(total_lines, len(clipped))
    result = [f"```sh\n{body}\n```"]
    if footer:
        result.append("")
        result.append(footer)
    return "\n".join(result)


def _format_multiline_code_block(
    text: str,
    *,
    language: str = "text",
    max_lines: int = 20,
    max_chars: int = 180,
    always_wrap: bool = False,
) -> str:
    stripped = text.strip()
    if not stripped or stripped.startswith("```"):
        return stripped
    lines = _shell_preview_lines(stripped) if language == "sh" else [
        line.rstrip() for line in stripped.splitlines() if line.strip()
    ]
    if len(lines) <= 1 and not always_wrap:
        return stripped
    clipped, total_lines = _clip_code_lines(
        lines,
        max_lines=max_lines,
        max_chars=max_chars,
    )
    body = "\n".join(clipped)
    footer = _preview_footer(total_lines, len(clipped))
    result = [f"```{language}\n{body}\n```"]
    if footer:
        result.extend(["", footer])
    return "\n".join(result)


def _format_tool_like_text(text: str, *, content_type: str) -> str:
    stripped = text.strip()
    if not stripped:
        return stripped
    if stripped.startswith("```") or _FENCED_BLOCK_RE.search(stripped):
        lines = stripped.splitlines()
        if any(_PREVIEW_FOOTER_RE.match(line.strip()) for line in lines):
            while lines and not lines[-1].strip():
                lines.pop()
            while lines and _REDUNDANT_OUTPUT_FOOTER_RE.match(lines[-1].strip()):
                lines.pop()
                while lines and not lines[-1].strip():
                    lines.pop()
            return "\n".join(lines).strip()
        return stripped
    if content_type == "file_change":
        return _format_file_change_block(stripped)
    if content_type == "tool_use":
        function_block = _format_function_call_json(stripped)
        if function_block:
            return function_block
    if content_type in {"tool_use", "tool_result"}:
        json_block = _format_json_code_block(stripped)
        if json_block:
            return json_block
        multiline_block = _format_multiline_code_block(
            stripped,
            language="text",
            max_lines=20,
            max_chars=180,
            always_wrap=(content_type == "tool_result"),
        )
        if multiline_block != stripped:
            return multiline_block
        if content_type == "tool_result":
            return _format_multiline_code_block(
                stripped,
                language="text",
                max_lines=20,
                max_chars=180,
                always_wrap=True,
            )
    return stripped


def format_response_text(
    text: str,
    *,
    is_complete: bool,
    content_type: str = "text",
    role: str = "assistant",
    for_history: bool = False,
) -> str:
    """Format a single normalized event for Telegram display."""
    text = text.strip()

    if role == "user":
        if not for_history and len(text) > 2996:
            text = text[:2996] + "…"
        return f"👤 {text}" if text else "👤"

    if content_type == "command_execution":
        text = _format_multiline_code_block(
            text,
            language="sh",
            max_lines=10,
            max_chars=180,
            always_wrap=False,
        )

    if content_type in {"tool_use", "tool_result", "file_change"}:
        text = _format_tool_like_text(text, content_type=content_type)

    if content_type in {"thinking", "reasoning"} and is_complete and not for_history:
        start_tag = TranscriptParser.EXPANDABLE_QUOTE_START
        end_tag = TranscriptParser.EXPANDABLE_QUOTE_END
        max_hidden = 500
        if start_tag in text and end_tag in text:
            inner = text[text.index(start_tag) + len(start_tag) : text.index(end_tag)]
            if len(inner) > max_hidden:
                inner = inner[:max_hidden] + "\n\n… (thinking truncated)"
            text = start_tag + inner + end_tag
        elif len(text) > max_hidden:
            text = text[:max_hidden] + "\n\n… (thinking truncated)"

    prefix = _CONTENT_PREFIXES.get(content_type, "")
    if prefix and text:
        return f"{prefix}\n{text}"
    if prefix:
        return prefix
    return text


def build_response_parts(
    text: str,
    is_complete: bool,
    content_type: str = "text",
    role: str = "assistant",
) -> list[str]:
    """Build paginated response messages for Telegram.

    Returns a list of raw markdown strings, each within Telegram's 4096 char limit.
    Multi-part messages get a [1/N] suffix.
    Markdown-to-MarkdownV2 conversion is done by the send layer, not here.
    """
    text = format_response_text(
        text,
        is_complete=is_complete,
        content_type=content_type,
        role=role,
    )

    if role == "user":
        return [text]

    # If text contains expandable quote sentinels, don't split —
    # the quote must stay atomic. Truncation is handled by
    # _render_expandable_quote in markdown_v2.py.
    if TranscriptParser.EXPANDABLE_QUOTE_START in text:
        return [text]

    # Convert tables to card-style before splitting so tables aren't broken
    # across messages. The send layer's convert_markdown() call is idempotent.
    text = convert_markdown_tables(text)

    # Split after formatting so content-type prefixes stay attached to the chunk
    # they describe. Use a conservative limit for MarkdownV2 expansion.
    text_chunks = split_message(text, max_length=3000)
    total = len(text_chunks)

    if total == 1:
        return [text_chunks[0]]

    parts = []
    for i, chunk in enumerate(text_chunks, 1):
        parts.append(f"{chunk}\n\n[{i}/{total}]")
    return parts


def build_commentary_parts(
    text: str,
    *,
    content_type: str = "commentary",
    role: str = "assistant",
) -> list[str]:
    """Build lossless commentary/orchestration pages near Telegram's size limit.

    Commentary is a visible artifact, not ephemeral status. Unlike
    ``build_status_text()``, this helper must preserve the full payload and only
    split when required by Telegram-sized chunks.
    """
    text = format_response_text(
        text,
        is_complete=True,
        content_type=content_type,
        role=role,
    )

    if role == "user":
        return [text]

    if TranscriptParser.EXPANDABLE_QUOTE_START in text:
        return [text]

    text = convert_markdown_tables(text)
    text_chunks = split_message(text, max_length=4000)
    total = len(text_chunks)

    if total == 1:
        return [text_chunks[0]]

    parts = []
    for i, chunk in enumerate(text_chunks, 1):
        parts.append(f"{chunk}\n\n[{i}/{total}]")
    return parts


def build_status_text(
    text: str,
    *,
    is_complete: bool,
    content_type: str = "text",
    role: str = "assistant",
) -> str:
    """Build a single Telegram status/progress string.

    Status messages are ephemeral. Keep them compact and strip expandable-quote
    sentinels so the mutable status artifact stays plain and editable.
    """
    formatted = format_response_text(
        text,
        is_complete=is_complete,
        content_type=content_type,
        role=role,
    )
    formatted = formatted.replace(TranscriptParser.EXPANDABLE_QUOTE_START, "")
    formatted = formatted.replace(TranscriptParser.EXPANDABLE_QUOTE_END, "")
    if len(formatted) > 3000:
        formatted = formatted[:3000].rstrip() + "…"
    return formatted
