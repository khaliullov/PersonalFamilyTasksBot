from datetime import datetime
import logging
from typing import Any, Dict, List, Optional

import aiosqlite

import database

logger = logging.getLogger("FamilyTasksBot.VoiceDB")


async def _init_voice_sqlite_conn(db: aiosqlite.Connection) -> None:
    """Инициализация схемы voice_messages для переданного aiosqlite connection."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_message_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            business_connection_id TEXT,
            sender_id INTEGER NOT NULL,
            telegram_file_id TEXT,
            audio_path TEXT,
            transcript TEXT,
            summary TEXT,
            category TEXT,
            processing_status TEXT NOT NULL DEFAULT 'pending',
            error_message TEXT,
            created_at TEXT NOT NULL,
            processed_at TEXT,
            UNIQUE(chat_id, telegram_message_id)
        )
        """
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_voice_messages_chat_msg
        ON voice_messages(chat_id, telegram_message_id)
        """
    )
    cursor = await db.execute("PRAGMA table_info(chat_history)")
    chat_columns = [row[1] for row in await cursor.fetchall()]

    if "source_message_id" not in chat_columns:
        await db.execute("ALTER TABLE chat_history ADD COLUMN source_message_id INTEGER")
        logger.info("Миграция: колонка 'source_message_id' добавлена в таблицу 'chat_history'.")

    if "message_type" not in chat_columns:
        await db.execute("ALTER TABLE chat_history ADD COLUMN message_type TEXT DEFAULT 'text'")
        logger.info("Миграция: колонка 'message_type' добавлена в таблицу 'chat_history'.")

    await db.commit()


async def init_voice_db(db_or_path: Any = None) -> None:
    """Инициализирует таблицу voice_messages (в PostgreSQL или SQLite)."""
    if db_or_path is not None and hasattr(db_or_path, "execute"):
        await _init_voice_sqlite_conn(db_or_path)
    else:
        path = db_or_path if isinstance(db_or_path, str) else None
        await database.init_db(path)


async def get_voice_message(
    chat_id: int,
    telegram_message_id: int,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Получает запись голосового сообщения по chat_id и telegram_message_id."""
    return await database.get_voice_message(chat_id, telegram_message_id, db_path)


async def create_voice_message(
    chat_id: int,
    telegram_message_id: int,
    sender_id: int,
    telegram_file_id: Optional[str] = None,
    business_connection_id: Optional[str] = None,
    audio_path: Optional[str] = None,
    db_path: Optional[str] = None,
) -> int:
    """Создает новую запись голосового сообщения со статусом 'pending'."""
    return await database.create_voice_message(
        chat_id=chat_id,
        telegram_message_id=telegram_message_id,
        sender_id=sender_id,
        telegram_file_id=telegram_file_id,
        business_connection_id=business_connection_id,
        audio_path=audio_path,
        db_path=db_path,
    )


async def update_voice_message(
    chat_id: int,
    telegram_message_id: int,
    status: Optional[str] = None,
    audio_path: Optional[str] = None,
    transcript: Optional[str] = None,
    summary: Optional[str] = None,
    category: Optional[str] = None,
    error_message: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Обновляет статус и данные голосового сообщения."""
    return await database.update_voice_message(
        chat_id=chat_id,
        telegram_message_id=telegram_message_id,
        status=status,
        audio_path=audio_path,
        transcript=transcript,
        summary=summary,
        category=category,
        error_message=error_message,
        db_path=db_path,
    )
