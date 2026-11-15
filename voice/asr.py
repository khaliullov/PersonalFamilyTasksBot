import asyncio
import logging
import os
import threading
from typing import Optional

from voice.config import (
    ASR_BEAM_SIZE,
    ASR_COMPUTE_TYPE,
    ASR_DEVICE,
    ASR_LANGUAGE,
    ASR_MODEL_SIZE,
    ASR_TIMEOUT_SECONDS,
)

logger = logging.getLogger("FamilyTasksBot.ASR")


class WhisperTranscriber:
    """
    Lazy-loading wrapper around faster-whisper.WhisperModel.
    Thread-safe model initialization and reuse across inference requests.
    """

    def __init__(
        self,
        model_size: str = ASR_MODEL_SIZE,
        device: str = ASR_DEVICE,
        compute_type: str = ASR_COMPUTE_TYPE,
        beam_size: int = ASR_BEAM_SIZE,
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self._model = None
        self._lock = threading.Lock()

    def get_model(self):
        """Lazily load the faster-whisper model."""
        if self._model is None:
            with self._lock:
                if self._model is None:
                    logger.info(
                        "Загрузка локальной модели ASR (faster-whisper: %s, device: %s, compute_type: %s)...",
                        self.model_size,
                        self.device,
                        self.compute_type,
                    )
                    from faster_whisper import WhisperModel

                    self._model = WhisperModel(
                        self.model_size,
                        device=self.device,
                        compute_type=self.compute_type,
                    )
                    logger.info("Модель ASR '%s' успешно загружена.", self.model_size)
        return self._model

    def transcribe_sync(
        self,
        audio_path: str,
        language: str = ASR_LANGUAGE,
        beam_size: Optional[int] = None,
    ) -> str:
        """
        Synchronous audio transcription.
        Must be executed in a worker thread to prevent blocking the asyncio event loop.
        """
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Файл аудио не найден: {audio_path}")

        file_size = os.path.getsize(audio_path)
        if file_size == 0:
            logger.warning("Аудиофайл пуст (0 байт).")
            return ""

        effective_beam = beam_size if beam_size is not None else self.beam_size
        model = self.get_model()

        # faster-whisper returns a generator of segments.
        # It must be iterated in this worker thread!
        segments, info = model.transcribe(
            audio_path,
            language=language,
            beam_size=effective_beam,
        )

        text_parts = [segment.text for segment in segments]
        full_transcript = " ".join("".join(text_parts).split()).strip()
        return full_transcript


_transcriber_instance: Optional[WhisperTranscriber] = None
_instance_lock = threading.Lock()


def get_transcriber() -> WhisperTranscriber:
    """Returns the singleton WhisperTranscriber instance."""
    global _transcriber_instance
    if _transcriber_instance is None:
        with _instance_lock:
            if _transcriber_instance is None:
                _transcriber_instance = WhisperTranscriber()
    return _transcriber_instance


def set_transcriber(transcriber: Optional[WhisperTranscriber]) -> None:
    """Overrides or resets the singleton transcriber (useful for testing and dependency injection)."""
    global _transcriber_instance
    with _instance_lock:
        _transcriber_instance = transcriber


async def transcribe_voice(
    audio_path: str,
    language: str = ASR_LANGUAGE,
) -> str:
    """
    Clean asynchronous interface for local speech-to-text transcription.
    Executes model inference in a separate worker thread to avoid blocking the event loop.
    """
    transcriber = get_transcriber()
    loop = asyncio.get_running_loop()

    try:
        transcript = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                transcriber.transcribe_sync,
                audio_path,
                language,
                transcriber.beam_size,
            ),
            timeout=float(ASR_TIMEOUT_SECONDS),
        )
        return transcript
    except asyncio.TimeoutError:
        logger.error(
            "Превышен таймаут транскрибации ASR (%d с).",
            ASR_TIMEOUT_SECONDS,
        )
        raise TimeoutError(f"ASR transcription timed out after {ASR_TIMEOUT_SECONDS}s")
    except Exception as e:
        logger.error("Ошибка при распознавании речи ASR: %s", type(e).__name__)
        raise
