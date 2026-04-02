"""Response message building for Telegram delivery.

Builds paginated response messages from Claude Code output:
  - Handles different content types (text, thinking, tool_use, tool_result)
  - Splits long messages into pages within Telegram's 4096 char limit
  - Truncates thinking content to keep messages compact

Markdown conversion is NOT done here — the send layer (message_sender,
message_queue) handles convert_markdown() so each message is converted
exactly once.

Key function:
  - build_response_parts: Build paginated response messages
"""

from ..markdown_v2 import convert_markdown_tables
from ..telegram_sender import split_message
from ..transcript_parser import TranscriptParser

_CONTENT_PREFIXES: dict[str, str] = {
    "thinking": "∴ Thinking…",
    "commentary": "ℹ Commentary",
    "reasoning": "∴ Reasoning…",
    "command_execution": "⌘ Command",
    "tool_use": "🛠 Tool",
    "tool_result": "↳ Tool Output",
    "file_change": "Δ Files",
}


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
