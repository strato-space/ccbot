"""Voice-to-text transcription via configurable ASR providers.

Default provider remains OpenAI's audio API.  Local ASR is integrated through a
host-configured command contract so ccbot does not import heavy torch/
transformers dependencies.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import config
from .delivery_audit import log_telegram_delivery

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None
_local_stt_semaphore: asyncio.Semaphore | None = None
_local_stt_semaphore_size: int | None = None
_OK_PATH_RE = re.compile(r"^OK:\s*(?P<path>.+?)\s*$", re.MULTILINE)


class TranscriptionConfigError(ValueError):
    """Raised when the selected STT provider is not configured."""


class LocalTranscriptionError(RuntimeError):
    """Raised when local command transcription fails."""


@dataclass(frozen=True)
class STTEvidence:
    provider: str
    status: str
    duration_seconds: float
    failure_class: str | None = None
    timeout: bool = False
    run_id: str | None = None
    text_len: int | None = None

    def audit_text(self) -> str:
        parts = [
            f"provider={self.provider}",
            f"status={self.status}",
            f"duration_ms={int(self.duration_seconds * 1000)}",
        ]
        if self.failure_class:
            parts.append(f"failure_class={self.failure_class}")
        if self.timeout:
            parts.append("timeout=true")
        if self.run_id:
            parts.append(f"run_id={self.run_id}")
        if self.text_len is not None:
            parts.append(f"text_len={self.text_len}")
        return " ".join(parts)


def _get_client() -> httpx.AsyncClient:
    """Return a lazily-initialized httpx client singleton."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


def _audit_stt(evidence: STTEvidence) -> None:
    log_telegram_delivery(
        action="stt_transcription",
        task_type="stt_ingress",
        content_type="voice_stt",
        semantic_kind="stt_lifecycle",
        text=evidence.audit_text(),
        success=evidence.status == "success",
        reason=evidence.failure_class,
    )


def _new_run_id() -> str:
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{uuid.uuid4().hex[:12]}"


def _get_local_stt_semaphore() -> asyncio.Semaphore:
    global _local_stt_semaphore, _local_stt_semaphore_size
    size = max(1, int(getattr(config, "local_stt_max_concurrency", 1) or 1))
    if _local_stt_semaphore is None or _local_stt_semaphore_size != size:
        _local_stt_semaphore = asyncio.Semaphore(size)
        _local_stt_semaphore_size = size
    return _local_stt_semaphore


def _format_local_command(
    *,
    input_path: Path,
    output_dir: Path,
    run_id: str,
) -> list[str]:
    template = getattr(config, "local_stt_command", "").strip()
    if not template:
        raise TranscriptionConfigError(
            "Voice transcription local_command provider requires CCBOT_LOCAL_STT_COMMAND."
        )
    try:
        tokens = shlex.split(template)
    except ValueError as exc:
        raise TranscriptionConfigError(
            f"Invalid CCBOT_LOCAL_STT_COMMAND: {exc}"
        ) from exc
    if not tokens:
        raise TranscriptionConfigError(
            "Voice transcription local_command provider requires CCBOT_LOCAL_STT_COMMAND."
        )
    mapping = {
        "input_path": str(input_path),
        "output_dir": str(output_dir),
        "model": getattr(config, "local_stt_model", "") or "",
        "language": getattr(config, "local_stt_language", "ru") or "ru",
        "run_id": run_id,
    }
    try:
        return [token.format(**mapping) for token in tokens]
    except (KeyError, ValueError) as exc:
        raise TranscriptionConfigError(
            f"Invalid CCBOT_LOCAL_STT_COMMAND placeholder: {exc}"
        ) from exc


def _contained_path(candidate: Path, output_dir: Path) -> Path:
    resolved_output = output_dir.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_output)
    except ValueError as exc:
        raise LocalTranscriptionError(
            "Local STT transcript path escaped the generated output directory."
        ) from exc
    return resolved_candidate


def _discover_transcript_path(stdout: str, input_path: Path, output_dir: Path) -> Path:
    candidates: list[Path] = []
    match = _OK_PATH_RE.search(stdout or "")
    if match:
        candidates.append(Path(match.group("path").strip()))
    candidates.append(output_dir / f"{input_path.stem}.txt")
    candidates.extend(sorted(output_dir.glob("*.txt")))

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            contained = _contained_path(candidate, output_dir)
        except LocalTranscriptionError:
            raise
        except OSError:
            continue
        if contained in seen:
            continue
        seen.add(contained)
        if contained.is_file():
            return contained
    raise LocalTranscriptionError(
        "Local STT command completed without a transcript file."
    )


async def transcribe_voice_openai(ogg_data: bytes) -> str:
    """Transcribe OGG voice data via OpenAI API."""
    start = time.monotonic()
    try:
        url = f"{config.openai_base_url.rstrip('/')}/audio/transcriptions"
        client = _get_client()
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {config.openai_api_key}"},
            files={"file": ("voice.ogg", ogg_data, "audio/ogg")},
            data={"model": "gpt-4o-transcribe"},
        )
        response.raise_for_status()

        text = response.json().get("text", "").strip()
        if not text:
            raise ValueError("Empty transcription returned by API")
        _audit_stt(
            STTEvidence(
                provider="openai",
                status="success",
                duration_seconds=time.monotonic() - start,
                text_len=len(text),
            )
        )
        return text
    except Exception as exc:
        _audit_stt(
            STTEvidence(
                provider="openai",
                status="failure",
                duration_seconds=time.monotonic() - start,
                failure_class=exc.__class__.__name__,
            )
        )
        raise


async def transcribe_voice_local_command(ogg_data: bytes) -> str:
    """Transcribe OGG voice data through the configured local command."""
    start = time.monotonic()
    run_id = _new_run_id()
    root = (config.config_dir / "media" / "stt" / run_id).resolve()
    output_dir = (root / "output").resolve()
    input_path = (root / "input.ogg").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(ogg_data)
    timeout = max(1, int(getattr(config, "local_stt_timeout_seconds", 300) or 300))
    keep_artifacts = bool(getattr(config, "local_stt_keep_artifacts", False))

    try:
        argv = _format_local_command(
            input_path=input_path,
            output_dir=output_dir,
            run_id=run_id,
        )
        semaphore = _get_local_stt_semaphore()
        async with semaphore:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, _stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout,
                )
            except TimeoutError as exc:
                proc.kill()
                await proc.communicate()
                raise TimeoutError(
                    f"Local STT command timed out after {timeout}s (run_id={run_id})."
                ) from exc
        stdout = stdout_bytes.decode("utf-8", "replace")
        if proc.returncode != 0:
            raise LocalTranscriptionError(
                "Local STT command failed "
                f"(exit={proc.returncode}, run_id={run_id})."
            )
        transcript_path = _discover_transcript_path(stdout, input_path, output_dir)
        text = transcript_path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError("Empty transcription returned by local STT command")
        _audit_stt(
            STTEvidence(
                provider="local_command",
                status="success",
                duration_seconds=time.monotonic() - start,
                run_id=run_id,
                text_len=len(text),
            )
        )
        return text
    except Exception as exc:
        _audit_stt(
            STTEvidence(
                provider="local_command",
                status="failure",
                duration_seconds=time.monotonic() - start,
                failure_class=exc.__class__.__name__,
                timeout=isinstance(exc, TimeoutError),
                run_id=run_id,
            )
        )
        raise
    finally:
        if not keep_artifacts:
            try:
                shutil.rmtree(root)
            except FileNotFoundError:
                pass
            except Exception:
                logger.debug(
                    "Failed to clean local STT run dir %s", run_id, exc_info=True
                )


async def transcribe_voice(ogg_data: bytes) -> str:
    """Transcribe OGG voice data through the configured provider.

    Raises:
        TranscriptionConfigError: selected provider lacks required configuration.
        httpx.HTTPStatusError: OpenAI API errors.
        TimeoutError: local command timeout.
        ValueError: provider returned an empty transcript.
        LocalTranscriptionError: local command failures.
    """
    provider = getattr(config, "voice_stt_provider", "openai") or "openai"
    if provider == "disabled":
        raise TranscriptionConfigError("Voice transcription is disabled.")
    if provider == "openai":
        if not config.openai_api_key:
            raise TranscriptionConfigError(
                "Voice transcription requires an OpenAI API key."
            )
        return await transcribe_voice_openai(ogg_data)
    if provider == "local_command":
        return await transcribe_voice_local_command(ogg_data)
    if provider == "auto":
        local_command = getattr(config, "local_stt_command", "").strip()
        if local_command:
            try:
                return await transcribe_voice_local_command(ogg_data)
            except Exception as local_exc:
                if not config.openai_api_key:
                    raise local_exc
                logger.info(
                    "Local STT failed in auto mode (%s); falling back to OpenAI",
                    local_exc.__class__.__name__,
                )
        if not config.openai_api_key:
            raise TranscriptionConfigError(
                "Voice transcription auto provider requires local command or OpenAI API key."
            )
        return await transcribe_voice_openai(ogg_data)
    raise TranscriptionConfigError(f"Unknown voice STT provider: {provider}")


def voice_transcription_config_error() -> str | None:
    """Return a user-facing provider configuration error, if any."""
    provider = getattr(config, "voice_stt_provider", "openai") or "openai"
    if provider == "disabled":
        return "Voice transcription is disabled."
    if provider == "openai" and not config.openai_api_key:
        return "Voice transcription requires an OpenAI API key."
    if (
        provider == "local_command"
        and not getattr(config, "local_stt_command", "").strip()
    ):
        return "Voice transcription local_command provider requires CCBOT_LOCAL_STT_COMMAND."
    if (
        provider == "auto"
        and not getattr(config, "local_stt_command", "").strip()
        and not config.openai_api_key
    ):
        return "Voice transcription auto provider requires local command or OpenAI API key."
    return None


async def close_client() -> None:
    """Close the httpx client (call on shutdown)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None
