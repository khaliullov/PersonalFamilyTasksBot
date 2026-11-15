import aiosqlite
import pytest

from bot import (
    get_active_tasks,
    get_recent_chat_history,
    get_task_by_id,
    init_db,
    save_chat_message,
)
from voice.db import (
    create_voice_message,
    get_voice_message,
    update_voice_message,
)


@pytest.mark.asyncio
async def test_legacy_database_migration(tmp_db):
    """
    Test 15: Existing database migration — existing tasks and history remain intact.
    Verify that an old database without new columns or voice_messages table
    is smoothly migrated without data loss.
    """
    # 1. Manually create legacy schema (v1 without voice_messages or extended columns)
    async with aiosqlite.connect(tmp_db) as db:
        await db.execute(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                deadline TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            INSERT INTO tasks (title, deadline, status, created_at)
            VALUES ('Старая важная задача', '2026-12-01 18:00:00', 'pending', '2026-10-10 12:00:00')
            """
        )

        await db.execute(
            """
            CREATE TABLE chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                sender_name TEXT,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            INSERT INTO chat_history (sender_id, sender_name, text, created_at)
            VALUES (11111, 'Жена', 'Привет, старое сообщение!', '2026-10-10 11:59:00')
            """
        )
        await db.commit()

    # 2. Run the modern init_db migration
    await init_db(tmp_db)

    # 3. Verify existing task was preserved and new columns are present
    old_task = await get_task_by_id(1, db_path=tmp_db)
    assert old_task is not None
    assert old_task["title"] == "Старая важная задача"
    assert old_task["status"] == "pending"
    assert "description" in old_task
    assert "updated_at" in old_task
    assert "trello_card_id" in old_task
    assert "trello_card_url" in old_task

    # 4. Verify existing chat history was preserved
    history = await get_recent_chat_history(limit=5, db_path=tmp_db)
    assert len(history) == 1
    assert history[0]["text"] == "Привет, старое сообщение!"

    # 5. Verify voice_messages table is ready to accept records
    v_id = await create_voice_message(
        chat_id=100,
        telegram_message_id=200,
        sender_id=11111,
        telegram_file_id="file_abc",
        db_path=tmp_db,
    )
    assert v_id is not None
    assert v_id > 0

    record = await get_voice_message(100, 200, db_path=tmp_db)
    assert record is not None
    assert record["processing_status"] == "pending"


@pytest.mark.asyncio
async def test_voice_message_crud_and_idempotency(initialized_db):
    """
    Test 4 & 9: Successful transcription saved in SQLite & Multiple voice messages - no duplicate records.
    """
    chat_id = 12345
    msg_id = 555
    sender_id = 999

    # 1. Create first time
    id1 = await create_voice_message(
        chat_id=chat_id,
        telegram_message_id=msg_id,
        sender_id=sender_id,
        telegram_file_id="file_voice_1",
        business_connection_id="biz_1",
        db_path=initialized_db,
    )
    assert id1 > 0

    # 2. Create second time (idempotency check) -> must return same ID, no duplicates
    id2 = await create_voice_message(
        chat_id=chat_id,
        telegram_message_id=msg_id,
        sender_id=sender_id,
        telegram_file_id="file_voice_1",
        db_path=initialized_db,
    )
    assert id1 == id2

    # 3. Update status to transcribing and completed
    updated = await update_voice_message(
        chat_id=chat_id,
        telegram_message_id=msg_id,
        status="transcribing",
        audio_path="/tmp/fake_audio.ogg",
        db_path=initialized_db,
    )
    assert updated is True

    record = await get_voice_message(chat_id, msg_id, db_path=initialized_db)
    assert record["processing_status"] == "transcribing"
    assert record["audio_path"] == "/tmp/fake_audio.ogg"

    # 4. Save transcript, summary, and category
    await update_voice_message(
        chat_id=chat_id,
        telegram_message_id=msg_id,
        status="completed",
        transcript="Запиши ребенка к стоматологу в субботу",
        summary="Жена просит записать ребенка к врачу",
        category="health",
        db_path=initialized_db,
    )

    completed_record = await get_voice_message(chat_id, msg_id, db_path=initialized_db)
    assert completed_record["processing_status"] == "completed"
    assert completed_record["transcript"] == "Запиши ребенка к стоматологу в субботу"
    assert completed_record["summary"] == "Жена просит записать ребенка к врачу"
    assert completed_record["category"] == "health"
    assert completed_record["processed_at"] is not None


@pytest.mark.asyncio
async def test_save_chat_message_with_voice_metadata(initialized_db):
    """
    Test 6: Transcription enters chat_history with source_message_id and message_type.
    """
    await save_chat_message(
        sender_id=11111,
        text="[голосовое сообщение]: Купи хлеб",
        sender_name="Жена [голосовое сообщение]",
        source_message_id=987,
        message_type="voice",
        db_path=initialized_db,
    )

    history = await get_recent_chat_history(limit=5, db_path=initialized_db)
    assert len(history) == 1
    assert history[0]["sender_name"] == "Жена [голосовое сообщение]"
    assert "[голосовое сообщение]: Купи хлеб" in history[0]["text"]
