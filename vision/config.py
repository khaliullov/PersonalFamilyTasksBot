"""Configuration for the local Vision subsystem."""

from dataclasses import dataclass
import os
import tempfile
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from vision.exceptions import VisionConfigError


def _bool_from_env(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None or not val.strip():
        return default
    lowered = val.strip().lower()
    if lowered in ("1", "true", "yes", "on", "y"):
        return True
    if lowered in ("0", "false", "no", "off", "n"):
        return False
    raise VisionConfigError(f"Некорректное булево значение для {key}: {val!r}")


def _int_from_env(key: str, default: int, min_value: int = 0, max_value: Optional[int] = None) -> int:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(raw.strip())
        except ValueError as exc:
            raise VisionConfigError(f"Некорректное целое значение для {key}: {raw!r}") from exc
    if value < min_value:
        raise VisionConfigError(f"{key} должно быть >= {min_value}, получено {value}")
    if max_value is not None and value > max_value:
        raise VisionConfigError(f"{key} должно быть <= {max_value}, получено {value}")
    return value


def _float_from_env(key: str, default: float, min_value: float = 0.0) -> float:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = float(raw.strip())
        except ValueError as exc:
            raise VisionConfigError(f"Некорректное числовое значение для {key}: {raw!r}") from exc
    if value < min_value:
        raise VisionConfigError(f"{key} должно быть >= {min_value}, получено {value}")
    return value


def normalize_ollama_base_url(raw_url: str) -> str:
    """Normalizes Ollama base URL by stripping known API suffixes and adapting Docker host."""
    url = (raw_url or "http://127.0.0.1:11434").strip().rstrip("/")
    for suffix in ("/api/generate", "/api/chat", "/api/tags"):
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
            break

    if not (url.startswith("http://") or url.startswith("https://")):
        raise VisionConfigError(f"Некорректный URL Ollama (ожидается http:// или https://): {url!r}")

    if (os.path.exists("/.dockerenv") or os.getenv("IN_DOCKER") == "1") and (
        "localhost" in url or "127.0.0.1" in url
    ):
        url = url.replace("localhost", "host.docker.internal").replace("127.0.0.1", "host.docker.internal")

    return url


@dataclass(frozen=True)
class VisionConfig:
    """Immutable configuration for local Vision analysis and photo-to-Trello integration."""

    enabled: bool = True
    provider: str = "ollama"
    model: str = "qwen3-vl:32b"
    base_url: str = "http://127.0.0.1:11434"
    timeout_seconds: int = 120

    max_image_size_mb: int = 20
    max_images_per_message: int = 10
    max_album_size: int = 10
    album_wait_seconds: float = 1.5
    max_concurrent_requests: int = 1

    # Preprocessing & safety limits
    max_image_pixels: int = 40_000_000  # ~40 MP decompression bomb guard
    max_image_dimension: int = 2048  # Preserves readable text while limiting inference size
    ocr_min_dimension: int = 2560  # Higher resolution ceiling for screenshots/documents/receipts
    jpeg_quality: int = 90

    # Temporary storage & retention
    temp_dir: str = os.path.join(tempfile.gettempdir(), "personalfamilytasksbot", "vision")
    keep_local_files: bool = False
    temp_file_ttl_minutes: int = 60

    # Context & Trello integration
    attach_to_trello: bool = True
    add_description_to_task: bool = True
    context_enabled: bool = True
    ocr_enabled: bool = True

    # Retry & background recovery
    retry_max_attempts: int = 3
    retry_base_delay_seconds: float = 2.0
    attachment_retry_interval_seconds: int = 120
    cleanup_interval_minutes: int = 15

    @property
    def max_image_size_bytes(self) -> int:
        return self.max_image_size_mb * 1024 * 1024

    def validate(self) -> "VisionConfig":
        if self.provider != "ollama":
            raise VisionConfigError(
                f"Неподдерживаемый VISION_PROVIDER={self.provider!r}. Поддерживается только локальный 'ollama'."
            )
        if not self.model or not self.model.strip():
            raise VisionConfigError("VISION_MODEL не может быть пустым.")
        if self.timeout_seconds < 1:
            raise VisionConfigError("VISION_TIMEOUT_SECONDS должен быть >= 1.")
        if self.max_image_size_mb < 1:
            raise VisionConfigError("VISION_MAX_IMAGE_SIZE_MB должен быть >= 1.")
        if self.max_images_per_message < 1:
            raise VisionConfigError("VISION_MAX_IMAGES_PER_MESSAGE должен быть >= 1.")
        if self.max_album_size < 1:
            raise VisionConfigError("VISION_MAX_ALBUM_SIZE должен быть >= 1.")
        if self.max_concurrent_requests < 1:
            raise VisionConfigError("VISION_MAX_CONCURRENT_REQUESTS должен быть >= 1.")
        if self.retry_max_attempts < 1:
            raise VisionConfigError("VISION_RETRY_MAX_ATTEMPTS должен быть >= 1.")
        if self.temp_file_ttl_minutes < 1:
            raise VisionConfigError("VISION_TEMP_FILE_TTL_MINUTES должен быть >= 1.")
        if not self.temp_dir or not self.temp_dir.strip():
            raise VisionConfigError("VISION_TEMP_DIR не может быть пустым.")
        return self


def load_vision_config() -> VisionConfig:
    """Loads and validates VisionConfig from environment variables, falling back to existing Ollama settings."""
    default_base_url = os.getenv("VISION_BASE_URL") or os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
    base_url = normalize_ollama_base_url(default_base_url)

    default_temp_dir = os.path.join(tempfile.gettempdir(), "personalfamilytasksbot", "vision")
    temp_dir = os.getenv("VISION_TEMP_DIR", default_temp_dir).strip() or default_temp_dir

    cfg = VisionConfig(
        enabled=_bool_from_env("VISION_ENABLED", True),
        provider=os.getenv("VISION_PROVIDER", "ollama").strip().lower(),
        model=os.getenv("VISION_MODEL", "qwen3-vl:32b").strip(),
        base_url=base_url,
        timeout_seconds=_int_from_env("VISION_TIMEOUT_SECONDS", 120, min_value=1, max_value=3600),
        max_image_size_mb=_int_from_env("VISION_MAX_IMAGE_SIZE_MB", 20, min_value=1, max_value=100),
        max_images_per_message=_int_from_env("VISION_MAX_IMAGES_PER_MESSAGE", 10, min_value=1, max_value=50),
        max_album_size=_int_from_env("VISION_MAX_ALBUM_SIZE", 10, min_value=1, max_value=50),
        album_wait_seconds=_float_from_env("VISION_ALBUM_WAIT_SECONDS", 1.5, min_value=0.05),
        max_concurrent_requests=_int_from_env("VISION_MAX_CONCURRENT_REQUESTS", 1, min_value=1, max_value=16),
        max_image_pixels=_int_from_env("VISION_MAX_IMAGE_PIXELS", 40_000_000, min_value=100_000),
        max_image_dimension=_int_from_env("VISION_MAX_IMAGE_DIMENSION", 2048, min_value=256, max_value=8192),
        ocr_min_dimension=_int_from_env("VISION_OCR_MAX_DIMENSION", 2560, min_value=512, max_value=8192),
        temp_dir=temp_dir,
        keep_local_files=_bool_from_env("VISION_KEEP_LOCAL_FILES", False),
        temp_file_ttl_minutes=_int_from_env("VISION_TEMP_FILE_TTL_MINUTES", 60, min_value=1, max_value=10080),
        attach_to_trello=_bool_from_env("VISION_ATTACH_TO_TRELLO", True),
        add_description_to_task=_bool_from_env("VISION_ADD_DESCRIPTION_TO_TASK", True),
        context_enabled=_bool_from_env("VISION_CONTEXT_ENABLED", True),
        ocr_enabled=_bool_from_env("VISION_OCR_ENABLED", True),
        retry_max_attempts=_int_from_env("VISION_RETRY_MAX_ATTEMPTS", 3, min_value=1, max_value=10),
        retry_base_delay_seconds=_float_from_env("VISION_RETRY_BASE_DELAY_SECONDS", 2.0, min_value=0.0),
        attachment_retry_interval_seconds=_int_from_env(
            "VISION_ATTACHMENT_RETRY_INTERVAL_SECONDS", 120, min_value=10, max_value=86400
        ),
        cleanup_interval_minutes=_int_from_env("VISION_CLEANUP_INTERVAL_MINUTES", 15, min_value=1, max_value=1440),
    )
    return cfg.validate()
