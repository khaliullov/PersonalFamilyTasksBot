import os
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from tests.conftest import make_message
from voice.db import get_voice_message
from voice.processor import edit_voice_message_caption, process_voice_message


@pytest.mark.asyncio
async def test_process_voice_message_full_success(initialized_db, mock_bot):
    """
    Test 2, 4, 5, 6:
    Voice message from WIFE_ID is transcribed, saved in SQLite and chat_history,
    summarized, and debounce is triggered.
    """
    chat_id = 100
    msg_id = 101
    msg = make_message(
        message_id=msg_id,
        chat_id=chat_id,
        is_voice=True,
    )

    saved_chat_messages = []

    async def mock_save_chat_message(sender_id, text, sender_name, source_message_id, message_type, db_path):
        saved_chat_messages.append({
            "sender_id": sender_id,
            "text": text,
            "sender_name": sender_name,
            "source_message_id": source_message_id,
            "message_type": message_type,
        })

    debounce_calls = []

    def mock_reset_debounce(uid):
        debounce_calls.append(uid)

    fake_transcript = "Купи пожалуйста хлеб и молоко к ужину"
    fake_summary = {
        "summary": "Жена просит купить хлеб и молоко к ужину.",
        "category": "shopping",
        "key_points": ["Хлеб", "Молоко к ужину"],
    }

    with patch("voice.processor.transcribe_voice", new=AsyncMock(return_value=fake_transcript)), \
         patch("voice.processor.generate_voice_summary", new=AsyncMock(return_value=fake_summary)):

        await process_voice_message(
            message=msg,
            bot=mock_bot,
            save_chat_message_fn=mock_save_chat_message,
            reset_debounce_fn=mock_reset_debounce,
            db_path=initialized_db,
        )

    # 1. Verify voice_messages table in SQLite
    record = await get_voice_message(chat_id, msg_id, db_path=initialized_db)
    assert record is not None
    assert record["processing_status"] == "completed"
    assert record["transcript"] == fake_transcript
    assert record["summary"] == fake_summary["summary"]
    assert record["category"] == "shopping"

    # 2. Verify chat_history was called
    assert len(saved_chat_messages) == 1
    assert saved_chat_messages[0]["text"] == fake_transcript
    assert saved_chat_messages[0]["sender_name"] == "Жена [голосовое сообщение]"
    assert saved_chat_messages[0]["message_type"] == "voice"

    # 3. Verify debounce was reset
    assert len(debounce_calls) == 1
    assert debounce_calls[0] == msg.from_user.id


@pytest.mark.asyncio
async def test_process_voice_message_failed_download(initialized_db, mock_bot):
    """
    Test 10: Failed Telegram download — graceful failure without crashing.
    """
    mock_bot.download_file = AsyncMock(side_effect=RuntimeError("Connection reset by peer"))

    msg = make_message(message_id=102, is_voice=True)
    save_mock = AsyncMock()
    debounce_mock = MagicMock()

    await process_voice_message(
        message=msg,
        bot=mock_bot,
        save_chat_message_fn=save_mock,
        reset_debounce_fn=debounce_mock,
        db_path=initialized_db,
    )

    record = await get_voice_message(msg.chat.id, msg.message_id, db_path=initialized_db)
    assert record is not None
    assert record["processing_status"] == "failed"
    assert "Download failed" in record["error_message"]
    save_mock.assert_not_called()
    debounce_mock.assert_not_called()


@pytest.mark.asyncio
async def test_process_voice_message_failed_asr(initialized_db, mock_bot):
    """
    Test 11: Failed ASR — graceful failure without crashing.
    """
    msg = make_message(message_id=103, is_voice=True)
    save_mock = AsyncMock()
    debounce_mock = MagicMock()

    with patch("voice.processor.transcribe_voice", new=AsyncMock(side_effect=RuntimeError("ASR model error"))):
        await process_voice_message(
            message=msg,
            bot=mock_bot,
            save_chat_message_fn=save_mock,
            reset_debounce_fn=debounce_mock,
            db_path=initialized_db,
        )

    record = await get_voice_message(msg.chat.id, msg.message_id, db_path=initialized_db)
    assert record is not None
    assert record["processing_status"] == "failed"
    assert "ASR failed" in record["error_message"]
    save_mock.assert_not_called()
    debounce_mock.assert_not_called()


@pytest.mark.asyncio
async def test_process_voice_message_failed_summary_preserves_transcript_and_task_extraction(
    initialized_db, mock_bot
):
    """
    Test 12: Failed Ollama summarization — transcript remains saved and task extraction remains possible.
    """
    msg = make_message(message_id=104, is_voice=True)
    saved_chat = []

    async def mock_save_chat_message(sender_id, text, sender_name, source_message_id, message_type, db_path):
        saved_chat.append(text)

    debounce_calls = []

    def mock_reset_debounce(uid):
        debounce_calls.append(uid)

    fake_transcript = "Купи молоко завтра"

    with patch("voice.processor.transcribe_voice", new=AsyncMock(return_value=fake_transcript)), \
         patch("voice.processor.generate_voice_summary", new=AsyncMock(return_value=None)):

        await process_voice_message(
            message=msg,
            bot=mock_bot,
            save_chat_message_fn=mock_save_chat_message,
            reset_debounce_fn=mock_reset_debounce,
            db_path=initialized_db,
        )

    record = await get_voice_message(msg.chat.id, msg.message_id, db_path=initialized_db)
    assert record is not None
    assert record["transcript"] == fake_transcript
    assert record["processing_status"] == "failed"
    assert "Summary generation failed" in record["error_message"]

    # Transcript was still added to chat_history and debounce was triggered!
    assert len(saved_chat) == 1
    assert saved_chat[0] == fake_transcript
    assert len(debounce_calls) == 1


@pytest.mark.asyncio
async def test_process_voice_message_file_size_exceeded(initialized_db, mock_bot):
    """Voice message exceeding max file size limit is rejected gracefully."""
    too_large_bytes = 35 * 1024 * 1024
    msg = make_message(message_id=105, is_voice=True, voice_file_size=too_large_bytes)
    save_mock = AsyncMock()

    with patch("voice.processor.ASR_MAX_FILE_SIZE_MB", 30):
        await process_voice_message(
            message=msg,
            bot=mock_bot,
            save_chat_message_fn=save_mock,
            reset_debounce_fn=MagicMock(),
            db_path=initialized_db,
        )

    record = await get_voice_message(msg.chat.id, msg.message_id, db_path=initialized_db)
    assert record is not None
    assert record["processing_status"] == "failed"
    assert "превышает допустимый лимит" in record["error_message"]
    save_mock.assert_not_called()


@pytest.mark.asyncio
async def test_caption_editing_in_edit_caption_mode(mock_bot):
    """
    Test 13: Telegram caption editing in edit_caption mode.
    """
    summary_data = {
        "summary": "Надо забрать посылку с почты.",
        "category": "household",
        "key_points": ["Забрать посылку"],
    }

    success = await edit_voice_message_caption(
        bot=mock_bot,
        chat_id=100,
        message_id=200,
        business_connection_id="biz_conn_xyz",
        existing_caption="Моя голосовая",
        summary_data=summary_data,
    )
    assert success is True
    mock_bot.edit_message_caption.assert_called_once()
    call_kwargs = mock_bot.edit_message_caption.call_args.kwargs
    assert call_kwargs["chat_id"] == 100
    assert call_kwargs["message_id"] == 200
    assert call_kwargs["business_connection_id"] == "biz_conn_xyz"
    assert "Моя голосовая" in call_kwargs["caption"]
    assert "Надо забрать посылку" in call_kwargs["caption"]


@pytest.mark.asyncio
async def test_caption_editing_failure_graceful(mock_bot):
    """
    Test 14: Telegram caption editing failure — error handled gracefully, no crash, no message sent.
    """
    mock_bot.edit_message_caption.side_effect = RuntimeError("Bad Request: message is not modified")

    summary_data = {
        "summary": "Просто тест.",
        "category": "other",
        "key_points": [],
    }

    success = await edit_voice_message_caption(
        bot=mock_bot,
        chat_id=100,
        message_id=201,
        business_connection_id="biz_conn_xyz",
        existing_caption=None,
        summary_data=summary_data,
    )
    assert success is False
    # Verify no fallback message was sent to the wife
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_process_voice_message_idempotency_skip(initialized_db, mock_bot):
    """
    Test 9: Calling process_voice_message on an already completed voice message does nothing.
    """
    msg = make_message(message_id=106, is_voice=True)

    with patch("voice.processor.transcribe_voice", new=AsyncMock(return_value="Первое")), \
         patch("voice.processor.generate_voice_summary", new=AsyncMock(return_value={"summary": "С", "category": "other", "key_points": []})):

        save_count = 0
        async def mock_save(*args, **kwargs):
            nonlocal save_count
            save_count += 1

        # First run -> completed
        await process_voice_message(
            message=msg,
            bot=mock_bot,
            save_chat_message_fn=mock_save,
            reset_debounce_fn=MagicMock(),
            db_path=initialized_db,
        )
        assert save_count == 1

        # Second run -> skipped
        await process_voice_message(
            message=msg,
            bot=mock_bot,
            save_chat_message_fn=mock_save,
            reset_debounce_fn=MagicMock(),
            db_path=initialized_db,
        )
        assert save_count == 1
