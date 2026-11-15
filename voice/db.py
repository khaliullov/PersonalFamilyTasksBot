from datetime import datetime
import json
import logging
import os
from typing import Any, Dict, List, Optional

import aiosqlite

logger = logging.getLogger("FamilyTasksBot.VoiceDB")


async def init_voice_db(db: aiosqlite.Connection) -> None:
    """
    Инициализирует таблицу voice_messages и применяет миграции к chat_history.
    Безопасно для существующих баз данных.
    """
    # 1. Создаем таблицу voice_messages
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

    # 2. Миграция таблицы chat_history: добавляем source_message_id и message_type
    cursor = await db.execute("PRAGMA table_info(chat_history)")
    chat_columns = [row[1] for row in await cursor.fetchall()]

    if "source_message_id" not in chat_columns:
        await db.execute("ALTER TABLE chat_history ADD COLUMN source_message_id INTEGER")
        logger.info("Миграция: колонка 'source_message_id' добавлена в таблицу 'chat_history'.")

    if "message_type" not in chat_columns:
        await db.execute("ALTER TABLE chat_history ADD COLUMN message_type TEXT DEFAULT 'text'")
        logger.info("Миграция: колонка 'message_type' добавлена в таблицу 'chat_history'.")

    await db.commit()


async def get_voice_message(
    chat_id: int,
    telegram_message_id: int,
    db_path: str,
) -> Optional[Dict[str, Any]]:
    """Получает запись голосового сообщения по chat_id и telegram_message_id."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, telegram_message_id, chat_id, business_connection_id,
                   sender_id, telegram_file_id, audio_path, transcript,
                   summary, category, processing_status, error_message,
                   created_at, processed_at
            FROM voice_messages
            WHERE chat_id = ? AND telegram_message_id = ?
            """,
            (chat_id, telegram_message_id),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def create_voice_message(
    chat_id: int,
    telegram_message_id: int,
    sender_id: int,
    telegram_file_id: Optional[str] = None,
    business_connection_id: Optional[str] = None,
    audio_path: Optional[str] = None,
    db_path: str = "tasks.db",
) -> int:
    """
    Создает новую запись голосового сообщения со статусом 'pending'.
    В случае повторной отправки (idempotency) возвращает ID существующей записи.
    """
    existing = await get_voice_message(chat_id, telegram_message_id, db_path)
    if existing:
        return existing["id"]

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            """
            INSERT INTO voice_messages (
                telegram_message_id, chat_id, business_connection_id,
                sender_id, telegram_file_id, audio_path,
                processing_status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                telegram_message_id,
                chat_id,
                business_connection_id,
                sender_id,
                telegram_file_id,
                audio_path,
                created_at,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def update_voice_message(
    chat_id: int,
    telegram_message_id: int,
    status: Optional[str] = None,
    audio_path: Optional[str] = None,
    transcript: Optional[str] = None,
    summary: Optional[str] = None,
    category: Optional[str] = None,
    error_message: Optional[str] = None,
    db_path: str = "tasks.db",
) -> bool:
    """Обновляет статус и данные голосового сообщения."""
    fields = []
    values: List[Any] = []

    if status is not None:
        fields.append("processing_status = ?")
        values.append(status)
        if status in ("completed", "failed"):
            fields.append("processed_at = ?")
            values.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    if audio_path is not None:
        fields.append("audio_path = ?")
        values.append(audio_path)

    if transcript is not None:
        fields.append("transcript = ?")
        values.append(transcript)

    if summary is not None:
        fields.append("summary = ?")
        values.append(summary)

    if category is not None:
        fields.append("category = ?")
        values.append(category)

    if error_message is not None:
        fields.append("error_message = ?")
        values.append(error_message)

    if not fields:
        return False

    values.extend([chat_id, telegram_message_id])
    query = f"UPDATE voice_messages SET {', '.join(fields)} WHERE chat_id = ? AND telegram_message_id = ?"

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(query, values)
        await db.commit()
        return cursor.rowcount > 0
