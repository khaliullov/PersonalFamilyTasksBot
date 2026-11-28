"""Safe image validation, preprocessing, and secure temporary file lifecycle utilities."""

import base64
from dataclasses import dataclass
import hashlib
import io
import logging
import os
import stat
import time
from typing import Optional, Tuple
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError

from vision.exceptions import VisionImageError

logger = logging.getLogger("FamilyTasksBot.VisionImageUtils")

# Supported formats detected strictly from actual file magic bytes / decoded header
SUPPORTED_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
    "GIF": ("image/gif", ".gif"),
    "BMP": ("image/bmp", ".bmp"),
    "TIFF": ("image/tiff", ".tiff"),
}

# Formats sent directly to Ollama vs converted to JPEG/PNG for compatibility
OLLAMA_NATIVE_FORMATS = {"JPEG", "PNG"}

# Max single side dimension safety guard (against pathological aspect ratios)
MAX_SINGLE_SIDE_PX = 16_000


@dataclass(frozen=True)
class ValidatedImageInfo:
    """Metadata produced after validating a downloaded image file."""

    file_path: str
    detected_format: str
    mime_type: str
    extension: str
    width: int
    height: int
    file_size: int
    sha256_hash: str


def detect_format_from_magic(header: bytes) -> Optional[str]:
    """Detects image format strictly from magic bytes (never trusts filename/MIME)."""
    if len(header) < 12:
        return None
    if header.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "WEBP"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "GIF"
    if header.startswith(b"BM"):
        return "BMP"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return "TIFF"
    return None


def ensure_secure_temp_dir(temp_dir: str) -> str:
    """Creates the temporary storage directory with restrictive 0o700 permissions."""
    abs_dir = os.path.abspath(temp_dir)
    os.makedirs(abs_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(abs_dir, stat.S_IRWXU)
    except OSError:
        # On some filesystems chmod may be restricted; directory still exists.
        pass
    return abs_dir


def allocate_temp_image_path(temp_dir: str, extension: str = ".img") -> str:
    """
    Allocates a randomized path inside temp_dir with zero user-controlled components.
    Protects against path traversal via commonpath verification.
    """
    safe_dir = ensure_secure_temp_dir(temp_dir)
    clean_ext = extension if extension in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".img"} else ".img"
    filename = f"vision_{uuid.uuid4().hex}{clean_ext}"
    full_path = os.path.abspath(os.path.join(safe_dir, filename))

    if os.path.commonpath([safe_dir, full_path]) != safe_dir:
        raise VisionImageError("Обнаружена некорректная попытка построения пути вне временного каталога.")

    # Pre-create file with restrictive permissions 0o600
    fd = os.open(full_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.close(fd)
    return full_path


def safe_remove_file(file_path: Optional[str]) -> bool:
    """Safely removes a temporary file if it exists; never raises."""
    if not file_path:
        return False
    try:
        if os.path.isfile(file_path):
            os.remove(file_path)
            return True
    except OSError as exc:
        logger.warning("Не удалось удалить временный файл изображения: %s", type(exc).__name__)
    return False


def cleanup_expired_temp_files(temp_dir: str, ttl_minutes: int) -> int:
    """Removes temporary vision files older than ttl_minutes from temp_dir."""
    if not temp_dir or not os.path.isdir(temp_dir):
        return 0
    cutoff = time.time() - (ttl_minutes * 60)
    removed = 0
    abs_dir = os.path.abspath(temp_dir)
    try:
        for entry in os.scandir(abs_dir):
            if not entry.is_file():
                continue
            if not entry.name.startswith("vision_"):
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    os.remove(entry.path)
                    removed += 1
            except OSError:
                continue
    except OSError as exc:
        logger.warning("Ошибка при очистке устаревших временных файлов Vision: %s", type(exc).__name__)
    return removed


def compute_sha256_file(file_path: str) -> str:
    """Computes SHA-256 hex digest of a file in chunks without loading everything into RAM."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def validate_image_file(
    file_path: str,
    *,
    max_size_bytes: int,
    max_pixels: int = 40_000_000,
) -> ValidatedImageInfo:
    """
    Validates a downloaded image on disk:
    - Non-empty and within byte size limit
    - Recognizable magic bytes (ignores extension/MIME)
    - Safe dimensions and pixel count (decompression bomb protection)
    - Decodable and not corrupted (Pillow verify + load check)
    """
    if not file_path or not os.path.isfile(file_path):
        raise VisionImageError("Файл изображения не найден на диске.")

    file_size = os.path.getsize(file_path)
    if file_size <= 0:
        raise VisionImageError("Получен пустой файл изображения (0 байт).")
    if file_size > max_size_bytes:
        raise VisionImageError(
            f"Размер изображения ({file_size} байт) превышает лимит ({max_size_bytes} байт)."
        )

    with open(file_path, "rb") as fh:
        header = fh.read(32)

    magic_format = detect_format_from_magic(header)
    if not magic_format or magic_format not in SUPPORTED_FORMATS:
        raise VisionImageError("Неподдерживаемый или некорректный формат файла изображения.")

    # Guard against decompression bombs via Pillow pixel limit
    prev_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with Image.open(file_path) as img:
            width, height = img.size
            detected_fmt = (img.format or magic_format).upper()
            if width <= 0 or height <= 0:
                raise VisionImageError("Изображение имеет некорректные размеры.")
            if width > MAX_SINGLE_SIDE_PX or height > MAX_SINGLE_SIDE_PX:
                raise VisionImageError(
                    f"Размеры изображения ({width}x{height}) превышают допустимый предел."
                )
            if width * height > max_pixels:
                raise VisionImageError(
                    f"Количество пикселей ({width * height}) превышает безопасный лимит ({max_pixels})."
                )
            if detected_fmt not in SUPPORTED_FORMATS:
                raise VisionImageError(f"Неподдерживаемый формат изображения: {detected_fmt}")
            img.verify()

        # Re-open and force full pixel decode to catch truncated/corrupted payloads
        with Image.open(file_path) as img_check:
            img_check.load()
    except Image.DecompressionBombError as exc:
        raise VisionImageError("Обнаружена потенциальная decompression bomb: слишком большое изображение.") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise VisionImageError(f"Поврежденное или нечитаемое изображение: {type(exc).__name__}") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = prev_limit

    mime_type, ext = SUPPORTED_FORMATS[detected_fmt]
    sha256_hash = compute_sha256_file(file_path)

    return ValidatedImageInfo(
        file_path=file_path,
        detected_format=detected_fmt,
        mime_type=mime_type,
        extension=ext,
        width=width,
        height=height,
        file_size=file_size,
        sha256_hash=sha256_hash,
    )


def preprocess_image_for_vision(
    file_path: str,
    *,
    max_dimension: int = 2048,
    ocr_max_dimension: int = 2560,
    max_pixels: int = 40_000_000,
    jpeg_quality: int = 90,
) -> Tuple[bytes, str]:
    """
    Prepares an image in memory for local Vision inference WITHOUT modifying the original file:
    - Applies EXIF orientation transpose
    - Strips EXIF/ancillary metadata by re-encoding clean pixel data
    - Preserves high resolution for tall/document/screenshot aspect ratios to keep OCR text legible
    - Converts unsupported formats (WEBP/BMP/GIF/TIFF/RGBA) to PNG or JPEG
    - Resizes only if exceeding safe dimension limits

    Returns (image_bytes, mime_type).
    """
    prev_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with Image.open(file_path) as raw_img:
            orig_format = (raw_img.format or "JPEG").upper()
            # Normalize orientation using EXIF before stripping metadata
            img = ImageOps.exif_transpose(raw_img)
            width, height = img.size

            # Detect likely document/screenshot/receipt (high contrast PNG or tall aspect ratio)
            aspect_ratio = max(width, height) / max(1, min(width, height))
            is_text_heavy_candidate = orig_format == "PNG" or aspect_ratio >= 1.6
            target_max_dim = max(max_dimension, ocr_max_dimension) if is_text_heavy_candidate else max_dimension

            needs_resize = max(width, height) > target_max_dim
            if needs_resize:
                scale = target_max_dim / float(max(width, height))
                new_size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
                img = img.resize(new_size, Image.Resampling.LANCZOS)

            # Choose output format: preserve PNG for PNGs (sharper text/screenshots), else JPEG
            output_buf = io.BytesIO()
            if orig_format == "PNG":
                if img.mode not in ("RGB", "RGBA", "L"):
                    img = img.convert("RGB")
                img.save(output_buf, format="PNG", optimize=True)
                return output_buf.getvalue(), "image/png"

            # Convert to RGB for clean JPEG output (handles WEBP, RGBA, P, CMYK, TIFF, BMP, GIF)
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                background = Image.new("RGB", img.size, (255, 255, 255))
                alpha_img = img.convert("RGBA")
                background.paste(alpha_img, mask=alpha_img.split()[-1])
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            img.save(output_buf, format="JPEG", quality=jpeg_quality, optimize=True)
            return output_buf.getvalue(), "image/jpeg"
    except Image.DecompressionBombError as exc:
        raise VisionImageError("Превышен лимит пикселей при предобработке изображения.") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise VisionImageError(f"Ошибка предобработки изображения: {type(exc).__name__}") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = prev_limit


def encode_image_base64(image_bytes: bytes) -> str:
    """Encodes image bytes to an ASCII base64 string for Ollama API."""
    return base64.b64encode(image_bytes).decode("ascii")
