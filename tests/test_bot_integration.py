import asyncio
from datetime import datetime, timedelta
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from apscheduler.schedulers.asyncio import AsyncIOScheduler
import bot as bot_module
from tests.conftest import make_message


@pytest.mark.asyncio
async def test_normal_text_message_existing_behavior(initialized_db, mock_bot):
    """
    Test 1: Normal Telegram text message — existing behavior unchanged.
    Text from WIFE_ID is saved to chat_history, debounced, and creates a task.
    """
    wife_msg = make_message(
        message_id=201,
        user_id=bot_module.WIFE_ID,
        text="Купи пожалуйста хлеб к ужину",
    )

    ollama_response = {
        "action": "create",
        "is_task": True,
        "task_id": None,
        "task_title": "Купить хлеб к ужину",
        "task_description": None,
        "has_deadline": False,
        "deadline_timestamp": None,
    }

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "DEBOUNCE_DELAY_SECONDS", 0.01), \
         patch.object(bot_module, "trello_service", None), \
         patch("bot.analyze_context_with_ollama", new=AsyncMock(return_value=ollama_response)):

        await bot_module.handle_wife_business_message(wife_msg, mock_bot)

        # Allow debounce task to execute
        await asyncio.sleep(0.05)

    # 1. Verify message in chat_history
    history = await bot_module.get_recent_chat_history(limit=5, db_path=initialized_db)
    assert len(history) == 1
    assert history[0]["text"] == "Купи пожалуйста хлеб к ужину"
    assert history[0]["sender_name"] == "Жена"

    # 2. Verify task created in SQLite
    tasks = await bot_module.get_active_tasks(db_path=initialized_db)
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Купить хлеб к ужину"

    # 3. Verify notification was sent to husband (MY_ID)
    mock_bot.send_message.assert_called_once()
    assert mock_bot.send_message.call_args.kwargs["chat_id"] == bot_module.MY_ID


@pytest.mark.asyncio
async def test_voice_message_from_another_sender_ignored(initialized_db, mock_bot):
    """
    Test 3: Voice message from another sender — ignored.
    """
    other_sender_id = 9999999
    other_msg = make_message(
        message_id=301,
        user_id=other_sender_id,
        is_voice=True,
    )

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch("voice.processor.process_voice_message") as mock_voice_proc:

        await bot_module.handle_wife_business_message(other_msg, mock_bot)
        mock_voice_proc.assert_not_called()

    # Verify no tasks, no chat history, no messages sent
    tasks = await bot_module.get_active_tasks(db_path=initialized_db)
    assert len(tasks) == 0
    history = await bot_module.get_recent_chat_history(limit=5, db_path=initialized_db)
    assert len(history) == 0
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_voice_message_with_task_creates_task(initialized_db, mock_bot):
    """
    Test 7: Voice message containing a task — creates a task.
    The transcription enters chat_history, context analysis detects the task,
    and a task record is created in SQLite.
    """
    voice_msg = make_message(
        message_id=401,
        user_id=bot_module.WIFE_ID,
        is_voice=True,
    )

    fake_transcript = "Запиши ребенка к стоматологу в субботу к 12:00"
    fake_summary = {
        "summary": "Жена просит записать ребенка к стоматологу.",
        "category": "health",
        "key_points": ["Стоматолог в субботу к 12:00"],
    }
    ollama_task_analysis = {
        "action": "create",
        "is_task": True,
        "task_id": None,
        "task_title": "Записать ребенка к стоматологу",
        "task_description": "Суббота к 12:00",
        "has_deadline": True,
        "deadline_timestamp": (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d 12:00:00"),
    }

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "DEBOUNCE_DELAY_SECONDS", 0.01), \
         patch.object(bot_module, "trello_service", None), \
         patch("voice.processor.transcribe_voice", new=AsyncMock(return_value=fake_transcript)), \
         patch("voice.processor.generate_voice_summary", new=AsyncMock(return_value=fake_summary)), \
         patch("bot.analyze_context_with_ollama", new=AsyncMock(return_value=ollama_task_analysis)):

        await bot_module.handle_wife_business_message(voice_msg, mock_bot)
        # Wait for debounce
        await asyncio.sleep(0.05)

    # 1. Verify task created
    tasks = await bot_module.get_active_tasks(db_path=initialized_db)
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Записать ребенка к стоматологу"
    assert "Суббота" in tasks[0]["description"]

    # 2. Verify chat history contains voice transcript
    history = await bot_module.get_recent_chat_history(limit=5, db_path=initialized_db)
    assert len(history) == 1
    assert fake_transcript in history[0]["text"]

    # 3. Verify notification to husband
    assert mock_bot.send_message.call_count >= 1


@pytest.mark.asyncio
async def test_voice_message_without_task_no_task_created(initialized_db, mock_bot):
    """
    Test 8: Voice message without a task — does not create a task.
    Summary is stored in SQLite voice_messages, but no task is inserted into tasks.
    """
    voice_msg = make_message(
        message_id=501,
        user_id=bot_module.WIFE_ID,
        is_voice=True,
    )

    fake_transcript = "Привет любимый, я просто соскучилась, хорошего тебе дня"
    fake_summary = {
        "summary": "Жена передает привет и желает хорошего дня.",
        "category": "family",
        "key_points": ["Приветствие", "Хороший день"],
    }
    ollama_no_task_analysis = {
        "action": "none",
        "is_task": False,
        "task_id": None,
        "task_title": None,
        "task_description": None,
        "has_deadline": False,
        "deadline_timestamp": None,
    }

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "DEBOUNCE_DELAY_SECONDS", 0.01), \
         patch("voice.processor.transcribe_voice", new=AsyncMock(return_value=fake_transcript)), \
         patch("voice.processor.generate_voice_summary", new=AsyncMock(return_value=fake_summary)), \
         patch("bot.analyze_context_with_ollama", new=AsyncMock(return_value=ollama_no_task_analysis)):

        await bot_module.handle_wife_business_message(voice_msg, mock_bot)
        await asyncio.sleep(0.05)

    # 1. No task created in SQLite
    tasks = await bot_module.get_active_tasks(db_path=initialized_db)
    assert len(tasks) == 0

    # 2. But voice message summary IS stored in voice_messages
    from voice.db import get_voice_message
    record = await get_voice_message(voice_msg.chat.id, voice_msg.message_id, db_path=initialized_db)
    assert record is not None
    assert record["processing_status"] == "completed"
    assert record["category"] == "family"
    assert "привет" in record["summary"].lower()


@pytest.mark.asyncio
async def test_apscheduler_reminders_continue_working(initialized_db, mock_bot):
    """
    Test 16: Existing APScheduler reminders continue working.
    """
    # 1. Create task with future deadline
    future_dt = datetime.now() + timedelta(hours=2)
    future_str = future_dt.strftime("%Y-%m-%d %H:%M:%S")

    task_id = await bot_module.create_task(
        title="Оплатить налоги",
        description="Квитанция на почте",
        deadline=future_str,
        db_path=initialized_db,
    )

    test_scheduler = AsyncIOScheduler()

    # 2. Schedule the job
    scheduled = await bot_module.schedule_task_job(
        scheduler_instance=test_scheduler,
        bot=mock_bot,
        task_id=task_id,
        title="Оплатить налоги",
        deadline_dt=future_dt,
        deadline_str=future_str,
    )
    assert scheduled is True

    job = test_scheduler.get_job(f"task_reminder_{task_id}")
    assert job is not None

    # 3. Simulate reminder firing callback
    await bot_module.send_task_reminder(
        bot=mock_bot,
        task_id=task_id,
        title="Оплатить налоги",
        deadline_str=future_str,
        my_id=bot_module.MY_ID,
        db_path=initialized_db,
    )

    # Verify notification sent and task marked 'reminded'
    mock_bot.send_message.assert_called_once()
    assert "НАПОМИНАНИЕ О ДЕДЛАЙНЕ" in mock_bot.send_message.call_args.kwargs["text"]

    updated_task = await bot_module.get_task_by_id(task_id, db_path=initialized_db)
    assert updated_task["status"] == "reminded"

    # 4. Test restore_scheduled_jobs with a new pending task
    another_dt = datetime.now() + timedelta(hours=3)
    another_str = another_dt.strftime("%Y-%m-%d %H:%M:%S")
    task2_id = await bot_module.create_task(
        title="Забрать заказ",
        deadline=another_str,
        db_path=initialized_db,
    )

    test_scheduler2 = AsyncIOScheduler()
    await bot_module.restore_scheduled_jobs(mock_bot, test_scheduler2, db_path=initialized_db)
    assert test_scheduler2.get_job(f"task_reminder_{task2_id}") is not None
