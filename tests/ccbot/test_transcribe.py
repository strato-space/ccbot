"""Unit tests for transcribe — voice-to-text via configurable ASR providers."""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ccbot import transcribe


@pytest.fixture(autouse=True)
def _clear_proxy_env(monkeypatch):
    """Prevent host proxy settings from affecting local httpx client creation."""
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _reset_client():
    """Ensure each test starts with a fresh client."""
    transcribe._client = None
    transcribe._local_stt_semaphore = None
    transcribe._local_stt_semaphore_size = None
    yield
    transcribe._client = None
    transcribe._local_stt_semaphore = None
    transcribe._local_stt_semaphore_size = None


@pytest.fixture
def mock_config():
    """Patch config with test values."""
    with patch.object(transcribe, "config") as cfg:
        cfg.openai_api_key = "sk-test-key"
        cfg.openai_base_url = "https://api.openai.com/v1"
        cfg.voice_stt_provider = "openai"
        yield cfg


def _mock_response(*, json_data: dict, status_code: int = 200) -> httpx.Response:
    """Build a fake httpx.Response."""
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
    resp = httpx.Response(status_code=status_code, json=json_data, request=request)
    return resp


class TestTranscribeVoice:
    @pytest.mark.asyncio
    async def test_success(self, mock_config):
        resp = _mock_response(json_data={"text": "Hello world"})
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
        ) as mock_post:
            result = await transcribe.transcribe_voice(b"fake-ogg-data")

        assert result == "Hello world"
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert "Bearer sk-test-key" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_empty_transcription_raises(self, mock_config):
        resp = _mock_response(json_data={"text": ""})
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
        ):
            with pytest.raises(ValueError, match="Empty transcription"):
                await transcribe.transcribe_voice(b"fake-ogg-data")

    @pytest.mark.asyncio
    async def test_whitespace_only_raises(self, mock_config):
        resp = _mock_response(json_data={"text": "   "})
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
        ):
            with pytest.raises(ValueError, match="Empty transcription"):
                await transcribe.transcribe_voice(b"fake-ogg-data")

    @pytest.mark.asyncio
    async def test_missing_text_field_raises(self, mock_config):
        resp = _mock_response(json_data={"result": "something"})
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
        ):
            with pytest.raises(ValueError, match="Empty transcription"):
                await transcribe.transcribe_voice(b"fake-ogg-data")

    @pytest.mark.asyncio
    async def test_api_error_raises(self, mock_config):
        resp = _mock_response(json_data={"error": "Unauthorized"}, status_code=401)
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
        ):
            with pytest.raises(httpx.HTTPStatusError):
                await transcribe.transcribe_voice(b"fake-ogg-data")

    @pytest.mark.asyncio
    async def test_custom_base_url(self, mock_config):
        mock_config.openai_base_url = "https://proxy.example.com/v1"
        resp = _mock_response(json_data={"text": "Transcribed"})
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
        ) as mock_post:
            result = await transcribe.transcribe_voice(b"fake-ogg-data")

        assert result == "Transcribed"
        url_arg = mock_post.call_args[0][0]
        assert url_arg == "https://proxy.example.com/v1/audio/transcriptions"


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        pid: int = 1234,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.pid = pid
        self.killed = False

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True


class TestLocalCommandTranscribe:
    @pytest.mark.asyncio
    async def test_local_command_success_with_paths_containing_spaces(
        self,
        mock_config,
        tmp_path,
    ):
        mock_config.voice_stt_provider = "local_command"
        mock_config.config_dir = tmp_path / "config with spaces"
        mock_config.local_stt_command = (
            "/bin/stt --input_path {input_path} --output_dir {output_dir} "
            "--model {model} --language {language}"
        )
        mock_config.local_stt_model = "antony66/whisper-large-v3-russian"
        mock_config.local_stt_language = "ru"
        mock_config.local_stt_timeout_seconds = 30
        mock_config.local_stt_max_concurrency = 1
        mock_config.local_stt_keep_artifacts = True

        async def _fake_exec(*argv, **_kwargs):
            output_dir = argv[argv.index("--output_dir") + 1]
            transcript_path = (
                tmp_path
                / "config with spaces"
                / "media"
                / "stt"
                / "run_safe"
                / "output"
                / "input.txt"
            )
            # The actual run id is patched below, so this path is deterministic.
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text("Привет из локальной модели\n", encoding="utf-8")
            assert "config with spaces" in output_dir
            assert "--input_path" in argv
            return _FakeProcess(stdout=f"OK: {transcript_path}\n".encode())

        with (
            patch("ccbot.transcribe._new_run_id", return_value="run_safe"),
            patch(
                "asyncio.create_subprocess_exec", side_effect=_fake_exec
            ) as mock_exec,
        ):
            text = await transcribe.transcribe_voice(b"ogg")

        assert text == "Привет из локальной модели"
        argv = mock_exec.await_args.args
        assert argv[0] == "/bin/stt"
        assert any("config with spaces" in arg for arg in argv)
        assert mock_exec.await_args.kwargs["start_new_session"] is True

    @pytest.mark.asyncio
    async def test_local_command_rejects_missing_command(self, mock_config, tmp_path):
        mock_config.voice_stt_provider = "local_command"
        mock_config.config_dir = tmp_path
        mock_config.local_stt_command = ""
        mock_config.local_stt_keep_artifacts = False

        with pytest.raises(
            transcribe.TranscriptionConfigError,
            match="CCBOT_LOCAL_STT_COMMAND",
        ):
            await transcribe.transcribe_voice(b"ogg")

    @pytest.mark.asyncio
    async def test_local_command_empty_transcript_raises(self, mock_config, tmp_path):
        mock_config.voice_stt_provider = "local_command"
        mock_config.config_dir = tmp_path
        mock_config.local_stt_command = (
            "/bin/stt --input_path {input_path} --output_dir {output_dir}"
        )
        mock_config.local_stt_model = "model"
        mock_config.local_stt_language = "ru"
        mock_config.local_stt_timeout_seconds = 30
        mock_config.local_stt_max_concurrency = 1
        mock_config.local_stt_keep_artifacts = False

        async def _fake_exec(*argv, **_kwargs):
            output_dir = argv[argv.index("--output_dir") + 1]
            transcript_path = tmp_path / "media/stt/run_empty/output/input.txt"
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text("  ", encoding="utf-8")
            return _FakeProcess(stdout=f"OK: {output_dir}/input.txt\n".encode())

        with (
            patch("ccbot.transcribe._new_run_id", return_value="run_empty"),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        ):
            with pytest.raises(ValueError, match="Empty transcription"):
                await transcribe.transcribe_voice(b"ogg")

    @pytest.mark.asyncio
    async def test_local_command_nonzero_hides_subprocess_output(
        self, mock_config, tmp_path
    ):
        mock_config.voice_stt_provider = "local_command"
        mock_config.config_dir = tmp_path
        mock_config.local_stt_command = (
            "/bin/stt --input_path {input_path} --output_dir {output_dir}"
        )
        mock_config.local_stt_model = "model"
        mock_config.local_stt_language = "ru"
        mock_config.local_stt_timeout_seconds = 30
        mock_config.local_stt_max_concurrency = 1
        mock_config.local_stt_keep_artifacts = False

        async def _fake_exec(*_argv, **_kwargs):
            return _FakeProcess(
                stderr=("token=secret " + "x" * 500).encode(),
                returncode=2,
            )

        with (
            patch("ccbot.transcribe._new_run_id", return_value="run_fail"),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        ):
            with pytest.raises(transcribe.LocalTranscriptionError) as exc:
                await transcribe.transcribe_voice(b"ogg")

        message = str(exc.value)
        assert "Local STT command failed" in message
        assert "exit=2" in message
        assert "run_id=run_fail" in message
        assert "token" not in message
        assert "secret" not in message
        assert "x" * 10 not in message
        assert len(message) < 380

    @pytest.mark.asyncio
    async def test_local_command_timeout_records_timeout(self, mock_config, tmp_path):
        mock_config.voice_stt_provider = "local_command"
        mock_config.config_dir = tmp_path
        mock_config.local_stt_command = (
            "/bin/stt --input_path {input_path} --output_dir {output_dir}"
        )
        mock_config.local_stt_model = "model"
        mock_config.local_stt_language = "ru"
        mock_config.local_stt_timeout_seconds = 1
        mock_config.local_stt_max_concurrency = 1
        mock_config.local_stt_keep_artifacts = False

        class _SlowProcess(_FakeProcess):
            async def communicate(self):
                await asyncio.sleep(10)
                return b"", b""

        with (
            patch("ccbot.transcribe._new_run_id", return_value="run_timeout"),
            patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=_SlowProcess(),
            ),
            patch("ccbot.transcribe._LOCAL_STT_KILL_WAIT_SECONDS", 0.01),
            patch("ccbot.transcribe.os.getpgid", return_value=4321) as mock_getpgid,
            patch("ccbot.transcribe.os.killpg") as mock_killpg,
            patch("ccbot.transcribe.log_telegram_delivery") as mock_audit,
        ):
            with pytest.raises(TimeoutError):
                await transcribe.transcribe_voice(b"ogg")

        mock_getpgid.assert_called_once_with(1234)
        mock_killpg.assert_called_once()
        assert any(
            "timeout=true" in call.kwargs["text"] for call in mock_audit.call_args_list
        )

    @pytest.mark.asyncio
    async def test_local_command_rejects_transcript_outside_output_dir(
        self,
        mock_config,
        tmp_path,
    ):
        mock_config.voice_stt_provider = "local_command"
        mock_config.config_dir = tmp_path
        mock_config.local_stt_command = (
            "/bin/stt --input_path {input_path} --output_dir {output_dir}"
        )
        mock_config.local_stt_model = "model"
        mock_config.local_stt_language = "ru"
        mock_config.local_stt_timeout_seconds = 30
        mock_config.local_stt_max_concurrency = 1
        mock_config.local_stt_keep_artifacts = False
        outside = tmp_path / "outside.txt"
        outside.write_text("do not read", encoding="utf-8")

        with (
            patch("ccbot.transcribe._new_run_id", return_value="run_escape"),
            patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=_FakeProcess(stdout=f"OK: {outside}\n".encode()),
            ),
        ):
            with pytest.raises(transcribe.LocalTranscriptionError, match="escaped"):
                await transcribe.transcribe_voice(b"ogg")

    @pytest.mark.asyncio
    async def test_auto_falls_back_to_openai_when_local_fails(
        self, mock_config, tmp_path
    ):
        mock_config.voice_stt_provider = "auto"
        mock_config.config_dir = tmp_path
        mock_config.local_stt_command = (
            "/bin/stt --input_path {input_path} --output_dir {output_dir}"
        )
        mock_config.local_stt_model = "model"
        mock_config.local_stt_language = "ru"
        mock_config.local_stt_timeout_seconds = 30
        mock_config.local_stt_max_concurrency = 1
        mock_config.local_stt_keep_artifacts = False
        mock_config.openai_api_key = "sk-test-key"
        resp = _mock_response(json_data={"text": "Fallback cloud"})

        with (
            patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=_FakeProcess(returncode=1, stderr=b"local failed"),
            ),
            patch.object(
                httpx.AsyncClient,
                "post",
                new_callable=AsyncMock,
                return_value=resp,
            ),
        ):
            assert await transcribe.transcribe_voice(b"ogg") == "Fallback cloud"

    @pytest.mark.asyncio
    async def test_auto_without_openai_reports_local_failure(
        self, mock_config, tmp_path
    ):
        mock_config.voice_stt_provider = "auto"
        mock_config.config_dir = tmp_path
        mock_config.local_stt_command = (
            "/bin/stt --input_path {input_path} --output_dir {output_dir}"
        )
        mock_config.local_stt_model = "model"
        mock_config.local_stt_language = "ru"
        mock_config.local_stt_timeout_seconds = 30
        mock_config.local_stt_max_concurrency = 1
        mock_config.local_stt_keep_artifacts = False
        mock_config.openai_api_key = ""

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=_FakeProcess(returncode=1, stderr=b"local failed"),
        ):
            with pytest.raises(transcribe.LocalTranscriptionError):
                await transcribe.transcribe_voice(b"ogg")

    @pytest.mark.asyncio
    async def test_base_url_trailing_slash_stripped(self, mock_config):
        mock_config.openai_base_url = "https://proxy.example.com/v1/"
        resp = _mock_response(json_data={"text": "OK"})
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
        ) as mock_post:
            await transcribe.transcribe_voice(b"fake-ogg-data")

        url_arg = mock_post.call_args[0][0]
        assert url_arg == "https://proxy.example.com/v1/audio/transcriptions"


class TestCloseClient:
    @pytest.mark.asyncio
    async def test_close_client_when_open(self):
        transcribe._client = httpx.AsyncClient()
        assert transcribe._client is not None
        await transcribe.close_client()
        assert transcribe._client is None

    @pytest.mark.asyncio
    async def test_close_client_when_none(self):
        assert transcribe._client is None
        await transcribe.close_client()
        assert transcribe._client is None
