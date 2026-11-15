import asyncio
import os
import tempfile
from unittest.mock import MagicMock, patch
import pytest

from voice.asr import (
    WhisperTranscriber,
    get_transcriber,
    set_transcriber,
    transcribe_voice,
)


@pytest.fixture(autouse=True)
def reset_transcriber_singleton():
    """Ensure clean transcriber singleton for each test."""
    yield
    set_transcriber(None)


@pytest.mark.asyncio
async def test_transcribe_voice_success():
    """Test successful async transcription with mocked model."""
    fake_transcriber = WhisperTranscriber()
    mock_model = MagicMock()
    mock_segment1 = MagicMock(text="Купи пожалуйста ")
    mock_segment2 = MagicMock(text="молоко и хлеб.")
    mock_model.transcribe.return_value = ([mock_segment1, mock_segment2], MagicMock())
    fake_transcriber._model = mock_model

    set_transcriber(fake_transcriber)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(b"fake audio data")
        temp_path = f.name

    try:
        result = await transcribe_voice(temp_path, language="ru")
        assert result == "Купи пожалуйста молоко и хлеб."
        mock_model.transcribe.assert_called_once()
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@pytest.mark.asyncio
async def test_transcribe_voice_empty_file():
    """Empty audio file returns empty string without calling model."""
    fake_transcriber = WhisperTranscriber()
    mock_model = MagicMock()
    fake_transcriber._model = mock_model
    set_transcriber(fake_transcriber)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        temp_path = f.name  # 0 bytes

    try:
        result = await transcribe_voice(temp_path)
        assert result == ""
        mock_model.transcribe.assert_not_called()
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@pytest.mark.asyncio
async def test_transcribe_voice_file_not_found():
    """Transcription of non-existent file raises FileNotFoundError."""
    fake_transcriber = WhisperTranscriber()
    set_transcriber(fake_transcriber)

    with pytest.raises(FileNotFoundError):
        await transcribe_voice("/non/existent/path/audio.ogg")


@pytest.mark.asyncio
async def test_transcribe_voice_timeout():
    """Transcription timeout raises TimeoutError."""
    fake_transcriber = WhisperTranscriber()

    def slow_transcribe(*args, **kwargs):
        import time
        time.sleep(1.0)
        return "late text"

    fake_transcriber.transcribe_sync = slow_transcribe
    set_transcriber(fake_transcriber)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(b"fake audio")
        temp_path = f.name

    try:
        with patch("voice.asr.ASR_TIMEOUT_SECONDS", 0.1):
            with pytest.raises(TimeoutError):
                await transcribe_voice(temp_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@pytest.mark.asyncio
async def test_transcribe_voice_error_propagation():
    """Model error is properly propagated."""
    fake_transcriber = WhisperTranscriber()
    mock_model = MagicMock()
    mock_model.transcribe.side_effect = RuntimeError("Corrupted audio frame")
    fake_transcriber._model = mock_model
    set_transcriber(fake_transcriber)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(b"corrupted data")
        temp_path = f.name

    try:
        with pytest.raises(RuntimeError):
            await transcribe_voice(temp_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def test_lazy_loading_transcriber():
    """WhisperModel is not instantiated until get_model() is called."""
    transcriber = WhisperTranscriber(model_size="tiny", device="cpu")
    assert transcriber._model is None

    with patch("faster_whisper.WhisperModel") as mock_whisper_cls:
        mock_instance = MagicMock()
        mock_whisper_cls.return_value = mock_instance

        model1 = transcriber.get_model()
        assert model1 is mock_instance
        mock_whisper_cls.assert_called_once_with("tiny", device="cpu", compute_type="int8")

        # Second call reuses existing model without re-instantiation
        model2 = transcriber.get_model()
        assert model2 is mock_instance
        assert mock_whisper_cls.call_count == 1
