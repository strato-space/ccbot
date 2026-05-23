"""Safe message sending helpers with MarkdownV2 fallback.

Provides utility functions for sending Telegram messages with automatic
format conversion and fallback to plain text on failure.

Uses telegramify-markdown for MarkdownV2 formatting.

Functions:
  - send_with_fallback: Send with formatting → plain text fallback
  - send_photo: Photo sending (single or media group)
  - safe_reply: Reply with formatting, fallback to plain text
  - safe_edit: Edit message with formatting → plain text fallback
  - safe_send: Send message with formatting → plain text fallback

Rate limiting is handled globally by AIORateLimiter on the Application.
RetryAfter exceptions are re-raised so callers (queue worker) can handle them.
"""

import io
import logging
from typing import Any

from telegram import (
    Bot,
    InputMediaDocument,
    InputMediaPhoto,
    LinkPreviewOptions,
    Message,
)
from telegram.error import RetryAfter

from ..markdown_v2 import convert_markdown
from ..transcript_parser import TranscriptParser

logger = logging.getLogger(__name__)


def strip_sentinels(text: str) -> str:
    """Strip expandable quote sentinel markers for plain text fallback."""
    for s in (
        TranscriptParser.EXPANDABLE_QUOTE_START,
        TranscriptParser.EXPANDABLE_QUOTE_END,
    ):
        text = text.replace(s, "")
    return text


def _ensure_formatted(text: str) -> str:
    """Convert markdown to MarkdownV2."""
    return convert_markdown(text)


PARSE_MODE = "MarkdownV2"


# Disable link previews in all messages to reduce visual noise
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def send_with_fallback(
    bot: Bot,
    chat_id: int,
    text: str,
    **kwargs: Any,
) -> Message | None:
    """Send message with MarkdownV2, falling back to plain text on failure.

    Returns the sent Message on success, None on failure.
    RetryAfter is re-raised for caller handling.
    """
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=_ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            return await bot.send_message(
                chat_id=chat_id, text=strip_sentinels(text), **kwargs
            )
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
            return None


async def send_photo(
    bot: Bot,
    chat_id: int,
    image_data: list[tuple[str, bytes]],
    caption: str | None = None,
    **kwargs: Any,
) -> Message | tuple[Message, ...] | None:
    """Send photo(s) to chat. Sends as media group if multiple images.

    Rate limiting is handled globally by AIORateLimiter on the Application.

    Args:
        bot: Telegram Bot instance
        chat_id: Target chat ID
        image_data: List of (media_type, raw_bytes) tuples
        **kwargs: Extra kwargs passed to send_photo/send_media_group
    """
    if not image_data:
        return None
    caption_kwargs: dict[str, Any] = {}
    if caption:
        caption_kwargs = {"caption": _ensure_formatted(caption), "parse_mode": PARSE_MODE}
    try:
        if len(image_data) == 1:
            _media_type, raw_bytes = image_data[0]
            return await bot.send_photo(
                chat_id=chat_id,
                photo=io.BytesIO(raw_bytes),
                **caption_kwargs,
                **kwargs,
            )
        media = []
        for index, (_media_type, raw_bytes) in enumerate(image_data):
            media_kwargs = caption_kwargs if index == 0 else {}
            media.append(
                InputMediaPhoto(media=io.BytesIO(raw_bytes), **media_kwargs)
            )
        return await bot.send_media_group(
            chat_id=chat_id,
            media=media,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception as formatted_error:
        if not caption:
            logger.error("Failed to send photo to %d: %s", chat_id, formatted_error)
            return None
        # Fall back to an unformatted caption. This preserves terminal media
        # delivery if MarkdownV2 conversion or Telegram parsing rejects the
        # formatted caption while still returning observable success/failure.
        try:
            if len(image_data) == 1:
                _media_type, raw_bytes = image_data[0]
                return await bot.send_photo(
                    chat_id=chat_id,
                    photo=io.BytesIO(raw_bytes),
                    caption=strip_sentinels(caption),
                    **kwargs,
                )
            media = []
            for index, (_media_type, raw_bytes) in enumerate(image_data):
                media_kwargs = (
                    {"caption": strip_sentinels(caption)} if index == 0 else {}
                )
                media.append(InputMediaPhoto(media=io.BytesIO(raw_bytes), **media_kwargs))
            return await bot.send_media_group(
                chat_id=chat_id,
                media=media,
                **kwargs,
            )
        except RetryAfter:
            raise
        except Exception as plain_error:
            logger.error(
                "Failed to send photo to %d: formatted=%s plain=%s",
                chat_id,
                formatted_error,
                plain_error,
            )
            return None


def _named_bytes_io(raw_bytes: bytes, filename: str) -> io.BytesIO:
    """Create a BytesIO with a name so Telegram preserves filenames."""
    bio = io.BytesIO(raw_bytes)
    bio.name = filename
    return bio


async def send_document(
    bot: Bot,
    chat_id: int,
    document_data: list[tuple[str, str, bytes]],
    **kwargs: Any,
) -> None:
    """Send document(s) to chat.

    Args:
        bot: Telegram Bot instance
        chat_id: Target chat ID
        document_data: List of (filename, media_type, raw_bytes) tuples
        **kwargs: Extra kwargs passed to send_document/send_media_group
    """
    if not document_data:
        return
    try:
        if len(document_data) == 1:
            filename, _media_type, raw_bytes = document_data[0]
            await bot.send_document(
                chat_id=chat_id,
                document=_named_bytes_io(raw_bytes, filename),
                filename=filename,
                **kwargs,
            )
        else:
            media = [
                InputMediaDocument(
                    media=_named_bytes_io(raw_bytes, filename),
                    filename=filename,
                )
                for filename, _media_type, raw_bytes in document_data
            ]
            await bot.send_media_group(
                chat_id=chat_id,
                media=media,
                **kwargs,
            )
    except RetryAfter:
        raise
    except Exception as e:
        logger.error("Failed to send document to %d: %s", chat_id, e)


async def safe_reply(message: Message, text: str, **kwargs: Any) -> Message:
    """Reply with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        return await message.reply_text(
            _ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            return await message.reply_text(strip_sentinels(text), **kwargs)
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to reply: {e}")
            raise


async def _edit_message_text(target: Any, text: str, **kwargs: Any) -> Message | None:
    """Edit a callback/message target using the PTB method it exposes."""
    edit_message_text = getattr(target, "edit_message_text", None)
    if callable(edit_message_text):
        return await edit_message_text(text, **kwargs)
    edit_text = getattr(target, "edit_text", None)
    if callable(edit_text):
        return await edit_text(text, **kwargs)
    raise AttributeError("target does not support text edits")


async def safe_edit(target: Any, text: str, **kwargs: Any) -> Message | None:
    """Edit message with formatting, returning None only after final failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        return await _edit_message_text(
            target,
            _ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            return await _edit_message_text(target, strip_sentinels(text), **kwargs)
        except RetryAfter:
            raise
        except Exception as e:
            logger.error("Failed to edit message: %s", e)
            return None


async def safe_send(
    bot: Bot,
    chat_id: int,
    text: str,
    message_thread_id: int | None = None,
    **kwargs: Any,
) -> Message | None:
    """Send message with formatting, returning None only after final failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    if message_thread_id is not None:
        kwargs.setdefault("message_thread_id", message_thread_id)
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=_ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            return await bot.send_message(
                chat_id=chat_id, text=strip_sentinels(text), **kwargs
            )
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
            return None
