"""Local Ollama Vision analyzer — strictly image analysis with zero privileged side effects."""

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, Optional, Tuple

import aiohttp
from pydantic import ValidationError

from vision.config import VisionConfig
from vision.exceptions import (
    VisionModelNotFoundError,
    VisionParseError,
    VisionTimeoutError,
    VisionUnavailableError,
)
from vision.image_utils import encode_image_base64, preprocess_image_for_vision
from vision.models import VisionAnalysisResult
from vision.prompts import VISION_SYSTEM_PROMPT, build_vision_user_prompt

logger = logging.getLogger("FamilyTasksBot.VisionAnalyzer")


def extract_vision_json(raw_response: str) -> Dict[str, Any]:
    """
    Strips <think>/<thought> blocks and markdown code fences, then extracts
    the JSON object from a Vision model response.
    """
    if not raw_response or not isinstance(raw_response, str):
        raise VisionParseError("Пустой ответ от Vision модели.")

    cleaned = re.sub(r"<thought[^>]*>.*?</thought>", "", raw_response, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<think[^>]*>.*?</think>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<thought[^>]*>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<think[^>]*>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()

    start_idx = cleaned.find("{")
    end_idx = cleaned.rfind("}")
    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        raise VisionParseError("В ответе Vision модели не найден валидный JSON-объект.")

    json_str = cleaned[start_idx : end_idx + 1].strip()
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise VisionParseError(f"Ошибка парсинга JSON ответа Vision модели: {exc.msg}") from exc

    if not isinstance(data, dict):
        raise VisionParseError("Распарсенный JSON ответа Vision модели не является объектом.")

    return data


class OllamaVisionAnalyzer:
    """
    Local Vision analyzer powered by Ollama.

    Strictly responsible for:
    - Preparing images safely off the event loop
    - Calling local Ollama multimodal API (/api/chat) with bounded concurrency & retries
    - Validating structured VisionAnalysisResult via Pydantic

    Has NO access to SQLite/PostgreSQL, Trello credentials, Telegram Bot, or task creation.
    """

    def __init__(
        self,
        config: VisionConfig,
        *,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self.config = config
        self._external_session = session
        self._session: Optional[aiohttp.ClientSession] = session
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)

    @property
    def chat_url(self) -> str:
        return f"{self.config.base_url}/api/chat"

    @property
    def tags_url(self) -> str:
        return f"{self.config.base_url}/api/tags"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        """Closes owned aiohttp session if created internally."""
        if self._session and not self._session.closed and self._external_session is None:
            await self._session.close()
            self._session = None

    async def check_model_available(self) -> Tuple[bool, str]:
        """
        Checks whether Ollama is reachable and whether the configured Vision model is installed.
        Never downloads or pulls models automatically.
        """
        session = await self._get_session()
        try:
            async with session.get(self.tags_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return False, f"Ollama /api/tags вернул HTTP {resp.status}"
                payload = await resp.json()
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return False, "Таймаут проверки локального Ollama (/api/tags)"
        except aiohttp.ClientError as exc:
            return False, f"Локальный Ollama недоступен ({type(exc).__name__})"
        except Exception as exc:
            return False, f"Ошибка проверки Ollama ({type(exc).__name__})"

        models = payload.get("models") if isinstance(payload, dict) else []
        installed_names = {
            str(m.get("name", "")).strip()
            for m in (models or [])
            if isinstance(m, dict) and m.get("name")
        }
        target = self.config.model.strip()
        target_with_latest = target if ":" in target else f"{target}:latest"

        if target in installed_names or target_with_latest in installed_names:
            return True, f"Модель '{target}' установлена в локальном Ollama"

        return (
            False,
            f"Модель '{target}' не найдена в Ollama. Установите вручную: ollama pull {target}",
        )

    async def _call_ollama_once(
        self,
        image_b64: str,
        user_prompt: str,
    ) -> VisionAnalysisResult:
        """Executes a single non-streaming /api/chat request against local Ollama."""
        # Do NOT pass "format": "json" — Ollama MLX runners return HTTP 501 for constrained decoding.
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": VISION_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": user_prompt,
                    "images": [image_b64],
                },
            ],
            "options": {
                "temperature": 0.1,
            },
        }

        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)

        try:
            async with session.post(self.chat_url, json=payload, timeout=timeout) as resp:
                if resp.status == 404:
                    err_body = (await resp.text())[:200]
                    if "not found" in err_body.lower() or "model" in err_body.lower():
                        raise VisionModelNotFoundError(
                            f"Vision модель '{self.config.model}' не установлена в Ollama. "
                            f"Выполните: ollama pull {self.config.model}"
                        )
                    raise VisionUnavailableError(f"Ollama вернул HTTP 404: эндпоинт недоступен.")

                if resp.status != 200:
                    err_body = (await resp.text())[:200]
                    if "not found" in err_body.lower() and "model" in err_body.lower():
                        raise VisionModelNotFoundError(
                            f"Vision модель '{self.config.model}' не установлена в Ollama. "
                            f"Выполните: ollama pull {self.config.model}"
                        )
                    raise VisionUnavailableError(f"Ollama вернул ошибку HTTP {resp.status}.")

                response_data = await resp.json()
        except asyncio.CancelledError:
            raise
        except (VisionModelNotFoundError, VisionUnavailableError):
            raise
        except asyncio.TimeoutError as exc:
            raise VisionTimeoutError(
                f"Таймаут Vision-инференса Ollama ({self.config.timeout_seconds} с)."
            ) from exc
        except aiohttp.ClientError as exc:
            raise VisionUnavailableError(
                f"Ошибка соединения с локальным Ollama: {type(exc).__name__}"
            ) from exc
        except Exception as exc:
            raise VisionUnavailableError(
                f"Непредвиденная ошибка HTTP-запроса к Ollama: {type(exc).__name__}"
            ) from exc

        if not isinstance(response_data, dict):
            raise VisionParseError("Ответ Ollama не является JSON-объектом.")

        if response_data.get("error"):
            err_msg = str(response_data["error"])
            if "not found" in err_msg.lower() and "model" in err_msg.lower():
                raise VisionModelNotFoundError(
                    f"Vision модель '{self.config.model}' не установлена в Ollama."
                )
            raise VisionUnavailableError("Ollama вернул ошибку в теле ответа.")

        raw_content = ""
        msg_obj = response_data.get("message")
        if isinstance(msg_obj, dict):
            raw_content = str(msg_obj.get("content") or "")
        elif "response" in response_data:
            raw_content = str(response_data.get("response") or "")

        parsed_dict = extract_vision_json(raw_content)

        # Enforce OCR disabled policy if configured
        if not self.config.ocr_enabled:
            parsed_dict["visible_text"] = []

        try:
            result = VisionAnalysisResult.model_validate(parsed_dict).ensure_summary()
        except ValidationError as exc:
            raise VisionParseError(
                f"Ответ Vision модели не прошел валидацию схемы ({exc.error_count()} ошибок)."
            ) from exc

        return result

    async def analyze_image(
        self,
        file_path: str,
        *,
        caption: Optional[str] = None,
        recent_context: Optional[str] = None,
    ) -> VisionAnalysisResult:
        """
        Preprocesses and analyzes a single image file via local Ollama with
        concurrency control and bounded retries for transient errors.
        """
        start_time = time.perf_counter()

        # Preprocess image off the event loop (CPU-bound PIL work) without modifying original file
        processed_bytes, processed_mime = await asyncio.to_thread(
            preprocess_image_for_vision,
            file_path,
            max_dimension=self.config.max_image_dimension,
            ocr_max_dimension=self.config.ocr_min_dimension,
            max_pixels=self.config.max_image_pixels,
            jpeg_quality=self.config.jpeg_quality,
        )
        image_b64 = await asyncio.to_thread(encode_image_base64, processed_bytes)
        user_prompt = build_vision_user_prompt(
            caption=caption,
            recent_context=recent_context,
            ocr_enabled=self.config.ocr_enabled,
        )

        max_attempts = max(1, self.config.retry_max_attempts)
        last_error: Optional[Exception] = None

        async with self._semaphore:
            for attempt in range(1, max_attempts + 1):
                try:
                    logger.info(
                        "Vision анализ через Ollama (модель=%s, попытка=%d/%d, mime=%s, bytes=%d)",
                        self.config.model,
                        attempt,
                        max_attempts,
                        processed_mime,
                        len(processed_bytes),
                    )
                    result = await self._call_ollama_once(image_b64, user_prompt)
                    elapsed = time.perf_counter() - start_time
                    logger.info(
                        "Vision анализ завершен за %.2f с (категория=%s, confidence=%.2f, "
                        "task_candidate=%s, ocr_lines=%d, uncertainties=%d)",
                        elapsed,
                        result.category.value,
                        result.confidence,
                        result.task_candidate,
                        len(result.visible_text),
                        len(result.uncertainties),
                    )
                    return result
                except asyncio.CancelledError:
                    logger.info("Vision анализ отменен (CancelledError).")
                    raise
                except VisionModelNotFoundError:
                    logger.error(
                        "Vision модель '%s' не установлена в локальном Ollama. "
                        "Установите вручную: ollama pull %s",
                        self.config.model,
                        self.config.model,
                    )
                    raise
                except (VisionUnavailableError, VisionTimeoutError, VisionParseError) as exc:
                    last_error = exc
                    if not exc.transient or attempt >= max_attempts:
                        logger.error(
                            "Vision анализ завершился ошибкой после %d попыток (%s).",
                            attempt,
                            type(exc).__name__,
                        )
                        raise
                    delay = self.config.retry_base_delay_seconds * (2 ** (attempt - 1))
                    logger.warning(
                        "Временная ошибка Vision (%s), повтор через %.1f с (попытка %d/%d)...",
                        type(exc).__name__,
                        delay,
                        attempt,
                        max_attempts,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error
