"""VisionProcessor — orchestrates Telegram image ingestion, albums, context, Trello attachments, and retries."""

import asyncio
import json
import logging
import os
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional, Sequence, Tuple

from aiogram import Bot, types

from vision.analyzer import OllamaVisionAnalyzer
from vision.config import VisionConfig
from vision import db as vision_db
from vision.exceptions import (
    VisionDownloadError,
    VisionError,
    VisionImageError,
    VisionModelNotFoundError,
    VisionParseError,
    VisionTimeoutError,
    VisionUnavailableError,
)
from vision.image_utils import (
    allocate_temp_image_path,
    cleanup_expired_temp_files,
    safe_remove_file,
    validate_image_file,
)
from vision.models import AlbumImageContext, VisionAnalysisResult
from vision.prompts import format_image_context_entry, format_trello_image_section

logger = logging.getLogger("FamilyTasksBot.VisionProcessor")

VISION_UNAVAILABLE_MESSAGE = (
    "Фото получила, но сейчас не смогла его проанализировать.\n"
    "Сохранила сообщение и попробую обработать позже."
)


def is_supported_image_message(message: types.Message) -> bool:
    """Returns True if the Telegram message carries a photo or an image document."""
    if getattr(message, "photo", None):
        return True
    doc = getattr(message, "document", None)
    if doc is not None:
        mime = (getattr(doc, "mime_type", None) or "").strip().lower()
        if mime.startswith("image/"):
            return True
    return False


def _extract_telegram_image_descriptor(
    message: types.Message,
) -> Optional[Tuple[str, Optional[str], int, str]]:
    """
    Extracts (file_id, file_unique_id, reported_file_size, kind) from a Telegram Message.
    For compressed photos, selects the highest-resolution variant (photo[-1]).
    """
    photos = getattr(message, "photo", None)
    if photos:
        best = photos[-1]
        return (
            best.file_id,
            getattr(best, "file_unique_id", None),
            int(getattr(best, "file_size", None) or 0),
            "photo",
        )

    doc = getattr(message, "document", None)
    if doc is not None:
        mime = (getattr(doc, "mime_type", None) or "").strip().lower()
        if mime.startswith("image/"):
            return (
                doc.file_id,
                getattr(doc, "file_unique_id", None),
                int(getattr(doc, "file_size", None) or 0),
                "document",
            )
    return None


def _sanitize_error_reason(exc: Exception) -> str:
    """Produces a concise error label without leaking credentials, paths, captions, or OCR."""
    name = type(exc).__name__
    if isinstance(exc, VisionModelNotFoundError):
        return f"{name}: configured model not installed"
    if isinstance(exc, VisionTimeoutError):
        return f"{name}: inference timeout"
    if isinstance(exc, VisionUnavailableError):
        return f"{name}: ollama unavailable"
    if isinstance(exc, VisionParseError):
        return f"{name}: invalid vision json"
    if isinstance(exc, VisionImageError):
        return f"{name}: invalid or unsupported image"
    if isinstance(exc, VisionDownloadError):
        return f"{name}: telegram download failed"
    return name


class VisionProcessor:
    """
    Coordinates end-to-end Telegram image handling without duplicating task extraction or Trello logic:
    - Enforces sender authorization (WIFE_ID) before any download or inference
    - Aggregates Telegram albums (media_group_id) into a single logical context event
    - Downloads and validates images into restrictive temporary storage
    - Invokes OllamaVisionAnalyzer (bounded concurrency, retries)
    - Persists compact image context into chat_history and triggers existing debouncer
    - Attaches original photos to Trello cards with deduplication and partial-failure retries
    - Manages temporary file cleanup and restart recovery
    """

    def __init__(
        self,
        config: VisionConfig,
        analyzer: OllamaVisionAnalyzer,
        *,
        authorized_user_id: int,
        owner_user_id: int,
        save_chat_message_fn: Callable[..., Coroutine[Any, Any, None]],
        get_recent_history_fn: Callable[..., Coroutine[Any, Any, List[Dict[str, Any]]]],
        reset_debounce_fn: Callable[[int, Bot], None],
        trello_service_fn: Optional[Callable[[], Any]] = None,
        db_path_fn: Optional[Callable[[], str]] = None,
        get_task_by_id_fn: Optional[Callable[..., Coroutine[Any, Any, Optional[Dict[str, Any]]]]] = None,
    ) -> None:
        self.config = config
        self.analyzer = analyzer
        self.authorized_user_id = authorized_user_id
        self.owner_user_id = owner_user_id
        self._save_chat_message = save_chat_message_fn
        self._get_recent_history = get_recent_history_fn
        self._reset_debounce = reset_debounce_fn
        self._trello_service_fn = trello_service_fn
        self._db_path_fn = db_path_fn or (lambda: "tasks.db")
        self._get_task_by_id = get_task_by_id_fn

        # Album collection state guarded by an async lock (prevents race conditions)
        self._album_lock = asyncio.Lock()
        self._album_buffers: Dict[Tuple[int, str], List[types.Message]] = {}
        self._album_timers: Dict[Tuple[int, str], asyncio.Task] = {}
        self._active_tasks: set[asyncio.Task] = set()

    def _resolve_db_path(self, db_path: Optional[str] = None) -> str:
        return db_path if db_path is not None else self._db_path_fn()

    def _resolve_trello_service(self) -> Any:
        return self._trello_service_fn() if self._trello_service_fn else None

    def _track_task(self, task: asyncio.Task) -> asyncio.Task:
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        return task

    async def wait_for_idle(self) -> None:
        """Waits for all in-flight album timers and background vision tasks to complete."""
        while True:
            pending = [t for t in list(self._album_timers.values()) + list(self._active_tasks) if not t.done()]
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)

    async def shutdown(self) -> None:
        """Cancels pending album timers and background vision jobs and closes HTTP resources."""
        for timer in list(self._album_timers.values()):
            if not timer.done():
                timer.cancel()
        self._album_timers.clear()
        self._album_buffers.clear()

        for task in list(self._active_tasks):
            if not task.done():
                task.cancel()
        if self._active_tasks:
            await asyncio.gather(*list(self._active_tasks), return_exceptions=True)
        self._active_tasks.clear()

        await self.analyzer.close()

    def is_authorized(self, message: types.Message) -> bool:
        """Strictly validates sender authorization using WIFE_ID before any media download."""
        return bool(message.from_user and message.from_user.id == self.authorized_user_id)

    async def enqueue_message(
        self,
        message: types.Message,
        bot: Bot,
        *,
        db_path: Optional[str] = None,
    ) -> Optional[asyncio.Task]:
        """
        Non-blocking entry point called from Telegram handler.
        Checks authorization immediately, buffers albums, or schedules single-photo processing
        as a background task so text/voice handlers are never blocked.
        """
        if not self.is_authorized(message):
            logger.debug("Фото от неавторизованного пользователя отклонено до скачивания.")
            return None

        if not self.config.enabled:
            logger.debug("Vision отключен (VISION_ENABLED=false), сообщение #%d пропущено.", message.message_id)
            return None

        if not is_supported_image_message(message):
            return None

        media_group_id = getattr(message, "media_group_id", None)
        if media_group_id:
            return await self._buffer_album_message(message, bot, str(media_group_id), db_path=db_path)

        task = asyncio.create_task(
            self._safe_run_batch([message], bot, db_path=db_path),
            name=f"vision_photo_{message.chat.id}_{message.message_id}",
        )
        return self._track_task(task)

    async def _buffer_album_message(
        self,
        message: types.Message,
        bot: Bot,
        media_group_id: str,
        *,
        db_path: Optional[str] = None,
    ) -> asyncio.Task:
        """Buffers album items without races and flushes them as one logical event."""
        key = (message.chat.id, media_group_id)
        async with self._album_lock:
            items = self._album_buffers.setdefault(key, [])
            existing_ids = {m.message_id for m in items}
            if message.message_id not in existing_ids:
                if len(items) < self.config.max_album_size:
                    items.append(message)
                else:
                    logger.warning(
                        "Альбом %s превысил лимит VISION_MAX_ALBUM_SIZE=%d; сообщение #%d пропущено.",
                        media_group_id,
                        self.config.max_album_size,
                        message.message_id,
                    )

            prev_timer = self._album_timers.get(key)
            if prev_timer and not prev_timer.done():
                prev_timer.cancel()

            timer_task = asyncio.create_task(
                self._flush_album_after_delay(key, bot, db_path=db_path),
                name=f"vision_album_{message.chat.id}_{media_group_id}",
            )
            self._album_timers[key] = timer_task
            self._track_task(timer_task)
            return timer_task

    async def _flush_album_after_delay(
        self,
        key: Tuple[int, str],
        bot: Bot,
        *,
        db_path: Optional[str] = None,
    ) -> None:
        try:
            await asyncio.sleep(self.config.album_wait_seconds)
        except asyncio.CancelledError:
            return

        async with self._album_lock:
            messages = self._album_buffers.pop(key, [])
            self._album_timers.pop(key, None)

        if messages:
            await self._safe_run_batch(messages, bot, db_path=db_path)

    async def _safe_run_batch(
        self,
        messages: Sequence[types.Message],
        bot: Bot,
        *,
        db_path: Optional[str] = None,
    ) -> None:
        try:
            await self.process_photo_batch(messages, bot, db_path=db_path)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Непредвиденная ошибка в VisionProcessor batch: %s", type(exc).__name__)

    async def _download_telegram_file(
        self,
        bot: Bot,
        file_id: str,
        *,
        expected_max_bytes: int,
    ) -> str:
        """Downloads a Telegram file into a randomized 0o600 temp file inside config.temp_dir."""
        temp_path = allocate_temp_image_path(self.config.temp_dir, extension=".img")
        try:
            tg_file = await bot.get_file(file_id)
            if not tg_file or not getattr(tg_file, "file_path", None):
                raise VisionDownloadError("Telegram API не вернул file_path для изображения.")
            tg_size = getattr(tg_file, "file_size", None)
            if tg_size and int(tg_size) > expected_max_bytes:
                raise VisionImageError(
                    f"Размер файла в Telegram ({tg_size} байт) превышает лимит ({expected_max_bytes} байт)."
                )
            await bot.download_file(tg_file.file_path, destination=temp_path)
            return temp_path
        except asyncio.CancelledError:
            safe_remove_file(temp_path)
            raise
        except (VisionImageError, VisionDownloadError):
            safe_remove_file(temp_path)
            raise
        except Exception as exc:
            safe_remove_file(temp_path)
            raise VisionDownloadError(f"Ошибка загрузки файла из Telegram: {type(exc).__name__}") from exc

    async def _build_recent_context_snippet(self, db_path: str) -> Optional[str]:
        """Builds a compact text-only snippet of recent dialogue history for Vision context."""
        try:
            recent = await self._get_recent_history(limit=4, db_path=db_path)
        except Exception:
            return None
        if not recent:
            return None
        lines: List[str] = []
        for msg in recent[-4:]:
            sender = msg.get("sender_name", "Собеседник")
            text = (msg.get("text") or "").strip()
            if text:
                lines.append(f"{sender}: {text[:220]}")
        return "\n".join(lines) if lines else None

    async def process_photo_batch(
        self,
        messages: Sequence[types.Message],
        bot: Bot,
        *,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Processes a single photo or a media-group album as ONE logical event:
        1. Validates authorization and sorts by message_id to preserve ordering.
        2. Enforces per-message idempotency via vision_images table.
        3. Downloads, validates, and analyzes each image locally via OllamaVisionAnalyzer.
        4. Stores one consolidated context entry into chat_history and triggers debouncer ONCE.
        5. Handles Vision failures gracefully without breaking text/caption processing.
        """
        if not self.config.enabled:
            return []

        target_db = self._resolve_db_path(db_path)

        # Defense in depth: filter strictly by authorized user and preserve ordering
        ordered = sorted(
            [m for m in messages if self.is_authorized(m) and is_supported_image_message(m)],
            key=lambda m: m.message_id,
        )
        if not ordered:
            return []

        limit = self.config.max_album_size if len(ordered) > 1 else self.config.max_images_per_message
        ordered = ordered[:limit]

        recent_ctx = await self._build_recent_context_snippet(target_db)
        analyzed_items: List[AlbumImageContext] = []
        processed_records: List[Dict[str, Any]] = []
        had_vision_outage = False
        first_sender_id = ordered[0].from_user.id

        for msg in ordered:
            chat_id = msg.chat.id
            msg_id = msg.message_id
            sender_id = msg.from_user.id
            media_group_id = getattr(msg, "media_group_id", None)
            caption = getattr(msg, "caption", None)
            biz_conn_id = getattr(msg, "business_connection_id", None)

            descriptor = _extract_telegram_image_descriptor(msg)
            if not descriptor:
                continue
            file_id, file_unique_id, reported_size, _kind = descriptor

            # 1. Idempotency check
            existing = await vision_db.get_vision_image(chat_id, msg_id, db_path=target_db)
            if existing and existing.get("vision_status") == "completed":
                logger.info("Изображение message_id=%d уже обработано ранее (idempotency skip).", msg_id)
                continue

            await vision_db.create_vision_image(
                chat_id=chat_id,
                telegram_message_id=msg_id,
                sender_id=sender_id,
                telegram_file_id=file_id,
                telegram_file_unique_id=file_unique_id,
                media_group_id=str(media_group_id) if media_group_id else None,
                business_connection_id=biz_conn_id,
                caption=caption,
                db_path=target_db,
            )

            # 2. Pre-download file size check
            if reported_size > self.config.max_image_size_bytes:
                logger.warning(
                    "Изображение message_id=%d отклонено: размер %d байт > лимит %d байт.",
                    msg_id,
                    reported_size,
                    self.config.max_image_size_bytes,
                )
                await vision_db.update_vision_image(
                    chat_id,
                    msg_id,
                    vision_status="failed",
                    last_error="VisionImageError: file size exceeds limit",
                    db_path=target_db,
                )
                continue

            # 3. Download to secure temporary file
            await vision_db.update_vision_image(
                chat_id,
                msg_id,
                vision_status="downloading",
                db_path=target_db,
            )
            local_path: Optional[str] = None
            try:
                dl_start = time.perf_counter()
                local_path = await self._download_telegram_file(
                    bot,
                    file_id,
                    expected_max_bytes=self.config.max_image_size_bytes,
                )
                logger.info(
                    "Изображение message_id=%d загружено из Telegram за %.2f с.",
                    msg_id,
                    time.perf_counter() - dl_start,
                )

                # 4. Validate content strictly off the event loop
                img_info = await asyncio.to_thread(
                    validate_image_file,
                    local_path,
                    max_size_bytes=self.config.max_image_size_bytes,
                    max_pixels=self.config.max_image_pixels,
                )

                safe_upload_name = f"photo_{msg_id}{img_info.extension}"
                await vision_db.update_vision_image(
                    chat_id,
                    msg_id,
                    local_file_path=local_path,
                    original_filename=safe_upload_name,
                    mime_type=img_info.mime_type,
                    file_size=img_info.file_size,
                    sha256_hash=img_info.sha256_hash,
                    vision_status="analyzing",
                    db_path=target_db,
                )
                logger.info(
                    "Изображение message_id=%d валидировано (format=%s, %dx%d, %d байт).",
                    msg_id,
                    img_info.detected_format,
                    img_info.width,
                    img_info.height,
                    img_info.file_size,
                )

                # 5. Local Vision inference via Ollama
                analysis = await self.analyzer.analyze_image(
                    local_path,
                    caption=caption,
                    recent_context=recent_ctx,
                )

                should_retain_for_trello = self.config.attach_to_trello or self.config.keep_local_files
                if not should_retain_for_trello:
                    safe_remove_file(local_path)
                    local_path = None

                ocr_joined = "\n".join(analysis.visible_text) if analysis.visible_text else None
                await vision_db.update_vision_image(
                    chat_id,
                    msg_id,
                    local_file_path=local_path,
                    clear_local_file_path=(local_path is None),
                    vision_status="completed",
                    analysis_json=analysis.model_dump_json(),
                    image_description=analysis.image_description,
                    short_summary=analysis.short_summary,
                    ocr_text=ocr_joined,
                    category=analysis.category.value,
                    confidence=analysis.confidence,
                    clear_last_error=True,
                    db_path=target_db,
                )

                analyzed_items.append(
                    AlbumImageContext(
                        message_id=msg_id,
                        file_unique_id=file_unique_id,
                        caption=caption,
                        analysis=analysis,
                    )
                )
                rec = await vision_db.get_vision_image(chat_id, msg_id, db_path=target_db)
                if rec:
                    processed_records.append(rec)

            except asyncio.CancelledError:
                safe_remove_file(local_path)
                await vision_db.update_vision_image(
                    chat_id,
                    msg_id,
                    vision_status="failed",
                    clear_local_file_path=True,
                    last_error="CancelledError",
                    db_path=target_db,
                )
                raise
            except VisionImageError as exc:
                safe_remove_file(local_path)
                logger.warning("Изображение message_id=%d отклонено при валидации: %s", msg_id, _sanitize_error_reason(exc))
                await vision_db.update_vision_image(
                    chat_id,
                    msg_id,
                    vision_status="failed",
                    clear_local_file_path=True,
                    last_error=_sanitize_error_reason(exc),
                    db_path=target_db,
                )
            except (VisionDownloadError, VisionUnavailableError, VisionTimeoutError, VisionModelNotFoundError, VisionParseError) as exc:
                safe_remove_file(local_path)
                had_vision_outage = True
                logger.error("Ошибка анализа изображения message_id=%d: %s", msg_id, _sanitize_error_reason(exc))
                await vision_db.update_vision_image(
                    chat_id,
                    msg_id,
                    vision_status="failed",
                    clear_local_file_path=True,
                    last_error=_sanitize_error_reason(exc),
                    db_path=target_db,
                )
            except Exception as exc:
                safe_remove_file(local_path)
                had_vision_outage = True
                logger.error("Непредвиденная ошибка обработки изображения message_id=%d: %s", msg_id, type(exc).__name__)
                await vision_db.update_vision_image(
                    chat_id,
                    msg_id,
                    vision_status="failed",
                    clear_local_file_path=True,
                    last_error=type(exc).__name__,
                    db_path=target_db,
                )

        # 6. Feed consolidated context into existing dialogue history & debouncer
        if analyzed_items and self.config.context_enabled:
            # Verify no duplicate context write for these messages
            unsaved_items: List[AlbumImageContext] = []
            for item in analyzed_items:
                rec = await vision_db.get_vision_image(ordered[0].chat.id, item.message_id, db_path=target_db)
                if not rec or not rec.get("context_saved"):
                    unsaved_items.append(item)

            if unsaved_items:
                context_text = format_image_context_entry(unsaved_items)
                is_album = len(unsaved_items) > 1 or bool(getattr(ordered[0], "media_group_id", None))
                sender_label = "Жена [альбом]" if is_album else "Жена [фото]"
                msg_type = "album" if is_album else "image"

                await self._save_chat_message(
                    sender_id=first_sender_id,
                    text=context_text,
                    sender_name=sender_label,
                    source_message_id=unsaved_items[0].message_id,
                    message_type=msg_type,
                    db_path=target_db,
                )
                for item in unsaved_items:
                    await vision_db.update_vision_image(
                        ordered[0].chat.id,
                        item.message_id,
                        context_saved=1,
                        db_path=target_db,
                    )
                logger.info(
                    "Контекст %s (%d изобр., first_msg_id=%d) добавлен в chat_history.",
                    msg_type,
                    len(unsaved_items),
                    unsaved_items[0].message_id,
                )
                self._reset_debounce(first_sender_id, bot)

        elif not analyzed_items and had_vision_outage:
            # Preserve any captions so normal text task pipeline still works during Vision outage!
            captions = [
                (m.message_id, m.caption.strip())
                for m in ordered
                if getattr(m, "caption", None) and m.caption.strip()
            ]
            if captions:
                combined_caption = "\n".join(c for _, c in captions)
                await self._save_chat_message(
                    sender_id=first_sender_id,
                    text=f"[фото не проанализировано] {combined_caption}",
                    sender_name="Жена [фото]",
                    source_message_id=captions[0][0],
                    message_type="image",
                    db_path=target_db,
                )
                self._reset_debounce(first_sender_id, bot)

            try:
                await bot.send_message(
                    chat_id=self.owner_user_id,
                    text=VISION_UNAVAILABLE_MESSAGE,
                )
            except Exception as exc:
                logger.error("Не удалось отправить уведомление о недоступности Vision: %s", type(exc).__name__)

        return processed_records

    async def find_unattached_images_for_history(
        self,
        recent_messages: List[Dict[str, Any]],
        *,
        allowed_message_ids: Optional[List[int]] = None,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Finds completed, unattached vision_images records corresponding to image/album
        entries present in the current dialogue context window.
        """
        target_db = self._resolve_db_path(db_path)
        source_ids = [
            int(m["source_message_id"])
            for m in recent_messages
            if m.get("source_message_id") is not None and m.get("message_type") in ("image", "album")
        ]
        if not source_ids:
            return []

        candidates = await vision_db.get_unattached_vision_images_for_messages(source_ids, db_path=target_db)
        if not candidates:
            return []

        # Also expand any media_group_id so every photo in an album is attached together!
        expanded: Dict[Tuple[int, int], Dict[str, Any]] = {}
        for rec in candidates:
            expanded[(rec["chat_id"], rec["telegram_message_id"])] = rec
            mg_id = rec.get("media_group_id")
            if mg_id:
                group_recs = await vision_db.get_vision_images_by_media_group(
                    rec["chat_id"], mg_id, db_path=target_db
                )
                for gr in group_recs:
                    if gr.get("vision_status") == "completed" and gr.get("task_id") is None:
                        expanded[(gr["chat_id"], gr["telegram_message_id"])] = gr

        result = sorted(expanded.values(), key=lambda r: r["telegram_message_id"])
        if allowed_message_ids is not None:
            allowed_set = {int(mid) for mid in allowed_message_ids}
            result = [r for r in result if r["telegram_message_id"] in allowed_set]
        return result

    @staticmethod
    def records_to_album_contexts(records: List[Dict[str, Any]]) -> List[AlbumImageContext]:
        """Reconstructs validated AlbumImageContext objects from vision_images DB records."""
        contexts: List[AlbumImageContext] = []
        for rec in records:
            raw_json = rec.get("analysis_json")
            if not raw_json:
                continue
            try:
                analysis = VisionAnalysisResult.model_validate_json(raw_json).ensure_summary()
            except Exception:
                continue
            contexts.append(
                AlbumImageContext(
                    message_id=rec["telegram_message_id"],
                    file_unique_id=rec.get("telegram_file_unique_id"),
                    caption=rec.get("caption"),
                    analysis=analysis,
                )
            )
        return contexts

    def build_trello_description_addition(self, records: List[Dict[str, Any]]) -> str:
        """Builds Markdown image info section for Trello cards if enabled."""
        if not self.config.add_description_to_task or not records:
            return ""
        contexts = self.records_to_album_contexts(records)
        if not contexts:
            return ""
        return format_trello_image_section(contexts)

    async def attach_images_to_task(
        self,
        *,
        task_id: int,
        trello_card_id: Optional[str],
        image_records: List[Dict[str, Any]],
        bot: Optional[Bot] = None,
        db_path: Optional[str] = None,
    ) -> Dict[str, int]:
        """
        Associates analyzed images with a task and uploads originals to Trello.
        Handles duplicates, missing local files (re-download fallback), and transient
        upload errors without ever rolling back task creation.
        """
        target_db = self._resolve_db_path(db_path)
        stats = {"linked": 0, "attached": 0, "skipped_duplicate": 0, "pending": 0, "failed": 0}
        if not image_records:
            return stats

        trello_svc = self._resolve_trello_service()
        can_upload = bool(
            self.config.attach_to_trello
            and trello_card_id
            and trello_svc is not None
            and getattr(trello_svc, "is_enabled", False)
        )

        for rec in image_records:
            chat_id = rec["chat_id"]
            msg_id = rec["telegram_message_id"]

            # Re-fetch freshest state for idempotency
            fresh = await vision_db.get_vision_image(chat_id, msg_id, db_path=target_db) or rec
            if fresh.get("attachment_status") == "attached" and fresh.get("trello_attachment_id"):
                stats["skipped_duplicate"] += 1
                continue

            await vision_db.update_vision_image(
                chat_id,
                msg_id,
                task_id=task_id,
                trello_card_id=trello_card_id,
                attachment_status="pending" if self.config.attach_to_trello else "none",
                db_path=target_db,
            )
            stats["linked"] += 1

            if not self.config.attach_to_trello:
                await self._cleanup_after_processing(fresh.get("local_file_path"), chat_id, msg_id, target_db)
                continue

            if not can_upload:
                stats["pending"] += 1
                continue

            # Duplicate prevention by content hash / file_unique_id on the same Trello card
            is_dup = await vision_db.has_duplicate_trello_attachment(
                trello_card_id=trello_card_id,
                sha256_hash=fresh.get("sha256_hash"),
                telegram_file_unique_id=fresh.get("telegram_file_unique_id"),
                db_path=target_db,
            )
            if is_dup:
                logger.info(
                    "Пропуск дублирующегося вложения для карточки Trello %s (message_id=%d).",
                    trello_card_id,
                    msg_id,
                )
                await vision_db.update_vision_image(
                    chat_id,
                    msg_id,
                    attachment_status="attached",
                    clear_last_error=True,
                    db_path=target_db,
                )
                await self._cleanup_after_processing(fresh.get("local_file_path"), chat_id, msg_id, target_db)
                stats["skipped_duplicate"] += 1
                continue

            # Ensure file is available locally (re-download via Telegram file_id if needed)
            file_path = fresh.get("local_file_path")
            redownloaded = False
            if not file_path or not os.path.isfile(file_path):
                file_id = fresh.get("telegram_file_id")
                if bot and file_id:
                    try:
                        file_path = await self._download_telegram_file(
                            bot,
                            file_id,
                            expected_max_bytes=self.config.max_image_size_bytes,
                        )
                        redownloaded = True
                        await vision_db.update_vision_image(
                            chat_id,
                            msg_id,
                            local_file_path=file_path,
                            db_path=target_db,
                        )
                    except Exception as exc:
                        new_retries = int(fresh.get("retry_count") or 0) + 1
                        await vision_db.update_vision_image(
                            chat_id,
                            msg_id,
                            attachment_status="failed",
                            retry_count=new_retries,
                            last_error=_sanitize_error_reason(exc),
                            db_path=target_db,
                        )
                        stats["failed"] += 1
                        continue
                else:
                    new_retries = int(fresh.get("retry_count") or 0) + 1
                    await vision_db.update_vision_image(
                        chat_id,
                        msg_id,
                        attachment_status="failed",
                        retry_count=new_retries,
                        last_error="LocalFileMissing",
                        db_path=target_db,
                    )
                    stats["failed"] += 1
                    continue

            await vision_db.update_vision_image(
                chat_id,
                msg_id,
                attachment_status="uploading",
                db_path=target_db,
            )

            filename = fresh.get("original_filename") or f"photo_{msg_id}.jpg"
            mime_type = fresh.get("mime_type") or "image/jpeg"

            try:
                attachment = await trello_svc.attach_image_to_card(
                    card_id=trello_card_id,
                    file_path=file_path,
                    filename=filename,
                    mime_type=mime_type,
                    max_attempts=self.config.retry_max_attempts,
                    base_delay_seconds=self.config.retry_base_delay_seconds,
                )
                if attachment and attachment.get("id"):
                    await vision_db.update_vision_image(
                        chat_id,
                        msg_id,
                        trello_attachment_id=str(attachment["id"]),
                        attachment_status="attached",
                        clear_last_error=True,
                        db_path=target_db,
                    )
                    await self._cleanup_after_processing(file_path, chat_id, msg_id, target_db)
                    stats["attached"] += 1
                else:
                    new_retries = int(fresh.get("retry_count") or 0) + 1
                    await vision_db.update_vision_image(
                        chat_id,
                        msg_id,
                        attachment_status="failed",
                        retry_count=new_retries,
                        last_error="TrelloAttachmentUploadFailed",
                        db_path=target_db,
                    )
                    if redownloaded and not self.config.keep_local_files:
                        # Retain only within TTL for next retry
                        pass
                    stats["failed"] += 1
            except Exception as exc:
                new_retries = int(fresh.get("retry_count") or 0) + 1
                logger.error(
                    "Ошибка прикрепления фото message_id=%d к карточке Trello %s: %s",
                    msg_id,
                    trello_card_id,
                    type(exc).__name__,
                )
                await vision_db.update_vision_image(
                    chat_id,
                    msg_id,
                    attachment_status="failed",
                    retry_count=new_retries,
                    last_error=type(exc).__name__,
                    db_path=target_db,
                )
                stats["failed"] += 1

        return stats

    async def _cleanup_after_processing(
        self,
        file_path: Optional[str],
        chat_id: int,
        message_id: int,
        db_path: str,
    ) -> None:
        """Removes temp file unless keep_local_files is enabled and clears DB path."""
        if not self.config.keep_local_files:
            if file_path:
                safe_remove_file(file_path)
            await vision_db.update_vision_image(
                chat_id,
                message_id,
                clear_local_file_path=True,
                db_path=db_path,
            )

    async def retry_pending_attachments(
        self,
        bot: Optional[Bot] = None,
        *,
        db_path: Optional[str] = None,
    ) -> Dict[str, int]:
        """Retries pending or failed Trello attachments with bounded attempts."""
        target_db = self._resolve_db_path(db_path)
        summary = {"checked": 0, "attached": 0, "failed": 0}
        if not self.config.enabled or not self.config.attach_to_trello:
            return summary

        trello_svc = self._resolve_trello_service()
        if not trello_svc or not getattr(trello_svc, "is_enabled", False):
            return summary

        pending = await vision_db.get_pending_trello_attachments(
            max_retries=self.config.retry_max_attempts,
            db_path=target_db,
        )
        summary["checked"] = len(pending)

        for rec in pending:
            task_id = rec.get("task_id")
            card_id = rec.get("trello_card_id")
            if not card_id and task_id and self._get_task_by_id:
                task = await self._get_task_by_id(task_id, db_path=target_db)
                if task and task.get("trello_card_id"):
                    card_id = task["trello_card_id"]
            if not card_id or not task_id:
                continue

            res = await self.attach_images_to_task(
                task_id=task_id,
                trello_card_id=card_id,
                image_records=[rec],
                bot=bot,
                db_path=target_db,
            )
            summary["attached"] += res["attached"]
            summary["failed"] += res["failed"]

        return summary

    async def recover_on_startup(self, *, db_path: Optional[str] = None) -> Dict[str, int]:
        """
        Recovers gracefully after a bot restart:
        - Marks any in-flight downloading/analyzing rows as failed (safe recovery)
        - Removes orphaned/expired temporary image files
        """
        target_db = self._resolve_db_path(db_path)
        interrupted = await vision_db.get_interrupted_vision_images(db_path=target_db)
        recovered = 0
        for rec in interrupted:
            safe_remove_file(rec.get("local_file_path"))
            await vision_db.update_vision_image(
                rec["chat_id"],
                rec["telegram_message_id"],
                vision_status="failed",
                clear_local_file_path=True,
                last_error="InterruptedByRestart",
                db_path=target_db,
            )
            recovered += 1

        cleaned = await asyncio.to_thread(
            cleanup_expired_temp_files,
            self.config.temp_dir,
            self.config.temp_file_ttl_minutes,
        )
        if recovered or cleaned:
            logger.info(
                "Восстановление Vision после перезапуска: прерванных=%d, удалено устаревших файлов=%d.",
                recovered,
                cleaned,
            )
        return {"recovered_interrupted": recovered, "cleaned_temp_files": cleaned}

    async def cleanup_temp_files(self) -> int:
        """Periodic job: removes expired temporary image files beyond TTL."""
        removed = await asyncio.to_thread(
            cleanup_expired_temp_files,
            self.config.temp_dir,
            self.config.temp_file_ttl_minutes,
        )
        if removed:
            logger.info("Очистка временных файлов Vision: удалено %d файлов.", removed)
        return removed
