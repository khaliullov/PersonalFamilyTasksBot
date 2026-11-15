import asyncio
from datetime import datetime
import html
import logging
import os
import tempfile
import time
from typing import Any, Callable, Coroutine, Dict, Optional

from aiogram import Bot, types

from voice.asr import transcribe_voice
from voice.config import (
    ASR_ENABLED,
    ASR_LANGUAGE,
    ASR_MAX_FILE_SIZE_MB,
    VOICE_AUDIO_DIR,
    VOICE_KEEP_AUDIO,
    VOICE_SUMMARY_MODE,
)
from voice.db import (
    create_voice_message,
    get_voice_message,
    update_voice_message,
)
from voice.summary import (
    format_summary_for_caption,
    generate_voice_summary,
)

logger = logging.getLogger("FamilyTasksBot.VoiceProcessor")

# Очередь / блокировка для предотвращения race conditions при параллельных голосовых
_voice_lock = asyncio.Lock()


async def edit_voice_message_caption(
    bot: Bot,
    chat_id: int,
    message_id: int,
    business_connection_id: Optional[str],
    existing_caption: Optional[str],
    summary_data: Dict[str, Any],
) -> bool:
    """
    Пытается отредактировать подпись исходного голосового сообщения в Telegram Business.
    Никогда не выбрасывает исключение наружу и не отправляет запасных сообщений.
    """
    try:
        max_caption_len = 1024
        existing_part = existing_caption.strip() if existing_caption else ""

        if existing_part:
            prefix_len = len(existing_part) + 2  # для двух переносов строк \n\n
            available_len = max(max_caption_len - prefix_len, 50)
            summary_part = format_summary_for_caption(summary_data, max_len=available_len)
            new_caption = f"{existing_part}\n\n{summary_part}"
        else:
            new_caption = format_summary_for_caption(summary_data, max_len=max_caption_len)

        if len(new_caption) > max_caption_len:
            new_caption = new_caption[: max_caption_len - 3] + "..."

        kwargs: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": new_caption,
        }
        if business_connection_id:
            kwargs["business_connection_id"] = business_connection_id

        await bot.edit_message_caption(**kwargs)
        logger.info("Подпись голосового сообщения #%d успешно обновлена (режим %s).", message_id, VOICE_SUMMARY_MODE)
        return True

    except Exception as e:
        logger.error(
            "Не удалось отредактировать подпись голосового сообщения #%d: %s",
            message_id,
            type(e).__name__,
        )
        return False


async def process_voice_message(
    message: types.Message,
    bot: Bot,
    save_chat_message_fn: Callable[..., Coroutine[Any, Any, None]],
    reset_debounce_fn: Callable[[int], None],
    db_path: str = "tasks.db",
    ollama_url: Optional[str] = None,
    ollama_model: Optional[str] = None,
    ollama_timeout: Optional[int] = None,
) -> None:
    """
    Основной пайплайн обработки голосового сообщения:
    1. Идемпотентность и проверка размера.
    2. Скачивание аудиофайла через Telegram Bot API.
    3. Локальное распознавание речи (faster-whisper).
    4. Сохранение транскрипции в SQLite и chat_history.
    5. Сброс таймера дебаунса для контекстного анализа задач.
    6. Локальная генерация структурированного резюме (Qwen 3.8).
    7. Сохранение резюме в SQLite.
    8. Опциональное редактирование подписи сообщения (editMessageCaption).
    """
    if not message.voice:
        logger.warning("Сообщение #%d не содержит голосовой дорожки voice.", message.message_id)
        return

    chat_id = message.chat.id
    message_id = message.message_id
    sender_id = message.from_user.id if message.from_user else chat_id
    file_id = message.voice.file_id
    file_size = message.voice.file_size or 0
    duration = message.voice.duration or 0

    logger.info(
        "Получено голосовое сообщение #%d от пользователя %d (длительность: %d с, размер: %d байт).",
        message_id,
        sender_id,
        duration,
        file_size,
    )

    if not ASR_ENABLED:
        logger.info("Обработка голосовых отключена (ASR_ENABLED=false). Сообщение #%d пропущено.", message_id)
        return

    # 1. Проверка идемпотентности
    existing = await get_voice_message(chat_id, message_id, db_path)
    if existing and existing.get("processing_status") == "completed":
        logger.info("Голосовое сообщение #%d уже было успешно обработано ранее. Пропуск.", message_id)
        return

    # 2. Создание или обновление первичной записи
    await create_voice_message(
        chat_id=chat_id,
        telegram_message_id=message_id,
        sender_id=sender_id,
        telegram_file_id=file_id,
        business_connection_id=message.business_connection_id,
        db_path=db_path,
    )

    # 3. Проверка размера файла
    max_bytes = ASR_MAX_FILE_SIZE_MB * 1024 * 1024
    if file_size > max_bytes:
        err_msg = f"Размер аудиофайла ({file_size} байт) превышает допустимый лимит ({max_bytes} байт)."
        logger.warning(err_msg)
        await update_voice_message(
            chat_id=chat_id,
            telegram_message_id=message_id,
            status="failed",
            error_message=err_msg,
            db_path=db_path,
        )
        return

    # Выполняем скачивание и ASR под блокировкой для последовательной обработки и порядка контекста
    async with _voice_lock:
        # Подготовка пути к файлу
        if VOICE_KEEP_AUDIO:
            os.makedirs(VOICE_AUDIO_DIR, exist_ok=True)
            audio_path = os.path.join(VOICE_AUDIO_DIR, f"{chat_id}_{message_id}_{message.voice.file_unique_id}.ogg")
            is_temp = False
        else:
            temp_file = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
            audio_path = temp_file.name
            temp_file.close()
            is_temp = True

        # 4. Скачивание аудиофайла
        await update_voice_message(
            chat_id=chat_id,
            telegram_message_id=message_id,
            status="downloading",
            audio_path=audio_path,
            db_path=db_path,
        )
        logger.info("Начало скачивания аудио для сообщения #%d...", message_id)

        try:
            download_start = time.perf_counter()
            tg_file = await bot.get_file(file_id)
            if not tg_file.file_path:
                raise RuntimeError("Telegram API не вернул file_path для аудиофайла.")
            await bot.download_file(tg_file.file_path, destination=audio_path)
            logger.info(
                "Аудио для сообщения #%d успешно скачано за %.2f с.",
                message_id,
                time.perf_counter() - download_start,
            )
        except Exception as e:
            logger.error("Ошибка при скачивании аудиосообщения #%d: %s", message_id, type(e).__name__)
            await update_voice_message(
                chat_id=chat_id,
                telegram_message_id=message_id,
                status="failed",
                error_message=f"Download failed: {type(e).__name__}",
                db_path=db_path,
            )
            if is_temp and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
            return

        # 5. Транскрибация речи (faster-whisper)
        await update_voice_message(
            chat_id=chat_id,
            telegram_message_id=message_id,
            status="transcribing",
            db_path=db_path,
        )
        logger.info("Начало локальной ASR транскрибации для сообщения #%d...", message_id)

        try:
            asr_start = time.perf_counter()
            transcript = await transcribe_voice(audio_path, language=ASR_LANGUAGE)
            asr_elapsed = time.perf_counter() - asr_start
            logger.info(
                "ASR транскрибация сообщения #%d завершена за %.2f с (распознано символов: %d).",
                message_id,
                asr_elapsed,
                len(transcript),
            )
        except Exception as e:
            logger.error("Ошибка при ASR транскрибации сообщения #%d: %s", message_id, type(e).__name__)
            await update_voice_message(
                chat_id=chat_id,
                telegram_message_id=message_id,
                status="failed",
                error_message=f"ASR failed: {type(e).__name__}",
                db_path=db_path,
            )
            return
        finally:
            # Очистка временного файла аудио, если сохранение не требуется
            if is_temp and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass

        # 6. Обработка пустой транскрипции (тишина)
        if not transcript or not transcript.strip():
            logger.info("Голосовое сообщение #%d содержит тишину/пустоту.", message_id)
            await update_voice_message(
                chat_id=chat_id,
                telegram_message_id=message_id,
                status="completed",
                transcript="",
                db_path=db_path,
            )
            return

        # 7. Сохранение транскрипции в SQLite
        await update_voice_message(
            chat_id=chat_id,
            telegram_message_id=message_id,
            transcript=transcript,
            db_path=db_path,
        )

        # 8. Добавление транскрипции в chat_history для пайплайна задач
        history_text = transcript
        if message.caption:
            history_text = f"{transcript}\n(Подпись: {message.caption})"

        await save_chat_message_fn(
            sender_id=sender_id,
            text=history_text,
            sender_name="Жена [голосовое сообщение]",
            source_message_id=message_id,
            message_type="voice",
            db_path=db_path,
        )
        logger.info("Транскрипция сообщения #%d сохранена в chat_history.", message_id)

        # 9. Сброс таймера дебаунса для извлечения задач
        reset_debounce_fn(sender_id)

    # 10. Генерация структурированного резюме через Qwen 3.8
    await update_voice_message(
        chat_id=chat_id,
        telegram_message_id=message_id,
        status="summarizing",
        db_path=db_path,
    )
    logger.info("Запуск генерации резюме для голосового сообщения #%d...", message_id)

    summary_start = time.perf_counter()
    summary_data = await generate_voice_summary(
        transcript=transcript,
        ollama_url=ollama_url,
        model=ollama_model,
        timeout_seconds=ollama_timeout,
    )
    summary_elapsed = time.perf_counter() - summary_start

    if not summary_data:
        logger.warning(
            "Генерация резюме для сообщения #%d завершилась неудачей за %.2f с. Транскрипция сохранена.",
            message_id,
            summary_elapsed,
        )
        await update_voice_message(
            chat_id=chat_id,
            telegram_message_id=message_id,
            status="failed",
            error_message="Summary generation failed",
            db_path=db_path,
        )
        return

    # 11. Сохранение готового резюме в SQLite
    await update_voice_message(
        chat_id=chat_id,
        telegram_message_id=message_id,
        status="completed",
        summary=summary_data.get("summary", ""),
        category=summary_data.get("category", "other"),
        db_path=db_path,
    )
    logger.info(
        "Резюме для сообщения #%d успешно сохранено за %.2f с (категория: %s).",
        message_id,
        summary_elapsed,
        summary_data.get("category", "other"),
    )

    # 12. Опциональное редактирование подписи Telegram Business
    if VOICE_SUMMARY_MODE == "edit_caption":
        logger.info("Попытка редактирования подписи голосового сообщения #%d в Telegram Business...", message_id)
        await edit_voice_message_caption(
            bot=bot,
            chat_id=chat_id,
            message_id=message_id,
            business_connection_id=message.business_connection_id,
            existing_caption=message.caption,
            summary_data=summary_data,
        )
