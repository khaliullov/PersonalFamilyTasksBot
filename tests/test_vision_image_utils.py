"""Tests for safe image validation, preprocessing, EXIF handling, decompression bomb guard, and temp file lifecycle."""

import io
import os
import stat
import time

from PIL import Image
import pytest

from vision.exceptions import VisionImageError
from vision.image_utils import (
    allocate_temp_image_path,
    cleanup_expired_temp_files,
    compute_sha256_file,
    detect_format_from_magic,
    preprocess_image_for_vision,
    safe_remove_file,
    validate_image_file,
)


def _save_image(path, fmt="JPEG", size=(300, 200), mode="RGB", color=(100, 150, 200), exif=None):
    img = Image.new(mode, size, color=color)
    kwargs = {}
    if exif is not None:
        kwargs["exif"] = exif
    img.save(path, format=fmt, **kwargs)
    return str(path)


def test_validate_valid_jpeg_png_webp(tmp_path):
    for fmt, ext, expected_mime in [
        ("JPEG", ".jpg", "image/jpeg"),
        ("PNG", ".png", "image/png"),
        ("WEBP", ".webp", "image/webp"),
    ]:
        p = _save_image(tmp_path / f"img{ext}", fmt=fmt)
        info = validate_image_file(p, max_size_bytes=5 * 1024 * 1024)
        assert info.detected_format == fmt
        assert info.mime_type == expected_mime
        assert info.width == 300
        assert info.height == 200
        assert len(info.sha256_hash) == 64


def test_validate_rejects_empty_file(tmp_path):
    p = tmp_path / "empty.jpg"
    p.write_bytes(b"")
    with pytest.raises(VisionImageError, match="пустой"):
        validate_image_file(str(p), max_size_bytes=1024 * 1024)


def test_validate_rejects_oversized_file(tmp_path):
    p = _save_image(tmp_path / "big.png", fmt="PNG", size=(200, 200))
    with pytest.raises(VisionImageError, match="превышает лимит"):
        validate_image_file(p, max_size_bytes=50)


def test_validate_rejects_spoofed_extension_and_non_image_content(tmp_path):
    fake = tmp_path / "malicious.jpg"
    fake.write_bytes(b"<html><script>alert(1)</script></html>")
    with pytest.raises(VisionImageError):
        validate_image_file(str(fake), max_size_bytes=1024 * 1024)


def test_validate_rejects_corrupted_jpeg(tmp_path):
    corrupted = tmp_path / "corrupt.jpg"
    # Valid JPEG SOI magic bytes followed by garbage
    corrupted.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00\xff" * 20)
    with pytest.raises(VisionImageError):
        validate_image_file(str(corrupted), max_size_bytes=1024 * 1024)


def test_validate_blocks_decompression_bomb(tmp_path):
    # Create a 500x500 PNG (250,000 px) and set max_pixels=100_000 to verify bomb protection
    p = _save_image(tmp_path / "bomb.png", fmt="PNG", size=(500, 500))
    with pytest.raises(VisionImageError):
        validate_image_file(p, max_size_bytes=10 * 1024 * 1024, max_pixels=100_000)


def test_preprocess_applies_exif_orientation_and_preserves_original(tmp_path):
    # Create a 200x100 JPEG with EXIF Orientation=6 (90 deg CW -> should become 100x200)
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation tag
    p = _save_image(tmp_path / "rotated.jpg", fmt="JPEG", size=(200, 100), exif=exif.tobytes())
    orig_hash = compute_sha256_file(p)

    out_bytes, mime = preprocess_image_for_vision(p, max_dimension=1024)
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(out_bytes)) as result_img:
        assert result_img.size == (100, 200)
        # EXIF metadata stripped
        assert len(result_img.getexif()) == 0

    # Original file on disk must be untouched (for Trello upload)
    assert compute_sha256_file(p) == orig_hash


def test_preprocess_resizes_large_image_and_preserves_ocr_resolution_for_png(tmp_path):
    # Large JPEG photo -> capped at max_dimension=1024
    jpg_path = _save_image(tmp_path / "huge.jpg", fmt="JPEG", size=(3000, 2000))
    jpg_bytes, jpg_mime = preprocess_image_for_vision(jpg_path, max_dimension=1024, ocr_max_dimension=2048)
    assert jpg_mime == "image/jpeg"
    with Image.open(io.BytesIO(jpg_bytes)) as img:
        assert max(img.size) == 1024

    # Large PNG screenshot -> uses higher ocr_max_dimension=2048 to preserve small text
    png_path = _save_image(tmp_path / "doc.png", fmt="PNG", size=(1800, 1200))
    png_bytes, png_mime = preprocess_image_for_vision(png_path, max_dimension=1024, ocr_max_dimension=2048)
    assert png_mime == "image/png"
    with Image.open(io.BytesIO(png_bytes)) as img:
        assert img.size == (1800, 1200)


def test_preprocess_converts_webp_rgba_to_jpeg(tmp_path):
    webp_path = _save_image(tmp_path / "sticker.webp", fmt="WEBP", size=(200, 200), mode="RGBA", color=(255, 0, 0, 128))
    out_bytes, mime = preprocess_image_for_vision(webp_path)
    assert mime == "image/jpeg"
    assert detect_format_from_magic(out_bytes[:16]) == "JPEG"


def test_temp_file_security_permissions_and_cleanup(tmp_path):
    temp_dir = str(tmp_path / "secure_vision")
    path = allocate_temp_image_path(temp_dir, extension=".jpg")

    assert os.path.dirname(path) == os.path.abspath(temp_dir)
    assert os.path.basename(path).startswith("vision_")
    assert os.path.isfile(path)

    # Check restrictive permissions on POSIX
    dir_mode = stat.S_IMODE(os.stat(temp_dir).st_mode)
    file_mode = stat.S_IMODE(os.stat(path).st_mode)
    assert dir_mode == 0o700
    assert file_mode == 0o600

    # Create an expired file and a fresh file
    old_path = allocate_temp_image_path(temp_dir, extension=".jpg")
    old_time = time.time() - 7200  # 2 hours ago
    os.utime(old_path, (old_time, old_time))

    removed = cleanup_expired_temp_files(temp_dir, ttl_minutes=60)
    assert removed == 1
    assert not os.path.exists(old_path)
    assert os.path.exists(path)

    assert safe_remove_file(path) is True
    assert not os.path.exists(path)
