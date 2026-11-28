"""Tests for Trello multipart photo attachments, deduplication, partial failure recovery, and logging hygiene."""

import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image
import pytest

import bot as bot_module
from trello.client import TrelloClient
from trello.service import TrelloSyncService
from vision import db as vision_db
from vision.analyzer import OllamaVisionAnalyzer
from vision.config import VisionConfig
from vision.image_utils import allocate_temp_image_path, compute_sha256_file
from vision.processor import VisionProcessor


def _write_temp_jpeg(temp_dir: str, color=(40, 120, 200)) -> tuple[str, str]:
    path = allocate_temp_image_path(temp_dir, extension=".jpg")
    img = Image.new("RGB", (120, 90), color=color)
    img.save(path, format="JPEG")
    return path, compute_sha256_file(path)


def _mock_aiohttp_session(status=200, json_data=None):
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    resp.text = AsyncMock(return_value="")

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock()
    session.closed = False
    session.get = MagicMock(return_value=cm)
    session.post = MagicMock(return_value=cm)
    return session


@pytest.mark.asyncio
async def test_trello_client_upload_card_attachment_multipart(tmp_path):
    file_path, _ = _write_temp_jpeg(str(tmp_path))
    fake_attachment = {
        "id": "att_12345",
        "name": "photo_10.jpg",
        "mimeType": "image/jpeg",
    }
    mock_session = _mock_aiohttp_session(status=200, json_data=fake_attachment)

    client = TrelloClient(api_key="SECRET_API_KEY", token="SECRET_TRELLO_TOKEN", session=mock_session)
    result = await client.upload_card_attachment(
        card_id="card_abc",
        file_path=file_path,
        filename="photo_10.jpg",
        mime_type="image/jpeg",
    )

    assert result is not None
    assert result["id"] == "att_12345"
    mock_session.post.assert_called_once()
    call_args = mock_session.post.call_args
    assert call_args.args[0].endswith("/cards/card_abc/attachments")
    # Multipart FormData is passed via `data`, NEVER as a URL string
    assert "data" in call_args.kwargs
    assert "url" not in call_args.kwargs.get("params", {})


@pytest.mark.asyncio
async def test_trello_service_attach_image_deduplicates_and_retries(tmp_path):
    file_path, _ = _write_temp_jpeg(str(tmp_path))
    mock_client = AsyncMock(spec=TrelloClient)
    mock_client.get_card_attachments = AsyncMock(return_value=[])
    # Fail once, then succeed on retry #2
    mock_client.upload_card_attachment = AsyncMock(
        side_effect=[None, {"id": "att_retry_ok", "name": "photo_1.jpg"}]
    )

    service = TrelloSyncService(
        api_key="k",
        token="t",
        board_id="b",
        client=mock_client,
    )

    with patch("trello.service.TRELLO_ENABLED", True):
        att = await service.attach_image_to_card(
            card_id="card_1",
            file_path=file_path,
            filename="photo_1.jpg",
            max_attempts=3,
            base_delay_seconds=0.0,
        )
    assert att == {"id": "att_retry_ok", "name": "photo_1.jpg"}
    assert mock_client.upload_card_attachment.call_count == 2

    # Second call sees existing attachment on Trello card -> no duplicate upload
    mock_client.get_card_attachments = AsyncMock(return_value=[{"id": "att_retry_ok", "name": "photo_1.jpg"}])
    mock_client.upload_card_attachment.reset_mock()
    with patch("trello.service.TRELLO_ENABLED", True):
        att2 = await service.attach_image_to_card(
            card_id="card_1",
            file_path=file_path,
            filename="photo_1.jpg",
        )
    assert att2["id"] == "att_retry_ok"
    mock_client.upload_card_attachment.assert_not_called()


@pytest.mark.asyncio
async def test_attach_images_duplicate_prevention_and_cleanup(initialized_db, tmp_path):
    temp_dir = str(tmp_path / "vision_tmp")
    path1, hash1 = _write_temp_jpeg(temp_dir, color=(10, 20, 30))
    path2, hash2 = _write_temp_jpeg(temp_dir, color=(10, 20, 30))  # Identical content -> same sha256!
    assert hash1 == hash2

    await vision_db.create_vision_image(
        chat_id=100,
        telegram_message_id=1,
        sender_id=bot_module.WIFE_ID,
        telegram_file_id="f1",
        telegram_file_unique_id="u1",
        db_path=initialized_db,
    )
    await vision_db.update_vision_image(
        100,
        1,
        local_file_path=path1,
        original_filename="photo_1.jpg",
        mime_type="image/jpeg",
        sha256_hash=hash1,
        vision_status="completed",
        db_path=initialized_db,
    )
    await vision_db.create_vision_image(
        chat_id=100,
        telegram_message_id=2,
        sender_id=bot_module.WIFE_ID,
        telegram_file_id="f2",
        telegram_file_unique_id="u2",
        db_path=initialized_db,
    )
    await vision_db.update_vision_image(
        100,
        2,
        local_file_path=path2,
        original_filename="photo_2.jpg",
        mime_type="image/jpeg",
        sha256_hash=hash2,
        vision_status="completed",
        db_path=initialized_db,
    )

    mock_trello = MagicMock()
    mock_trello.is_enabled = True
    mock_trello.attach_image_to_card = AsyncMock(return_value={"id": "att_001"})

    cfg = VisionConfig(temp_dir=temp_dir, keep_local_files=False, retry_base_delay_seconds=0.0)
    processor = VisionProcessor(
        config=cfg,
        analyzer=OllamaVisionAnalyzer(cfg),
        authorized_user_id=bot_module.WIFE_ID,
        owner_user_id=bot_module.MY_ID,
        save_chat_message_fn=AsyncMock(),
        get_recent_history_fn=AsyncMock(return_value=[]),
        reset_debounce_fn=MagicMock(),
        trello_service_fn=lambda: mock_trello,
        db_path_fn=lambda: initialized_db,
    )

    rec1 = await vision_db.get_vision_image(100, 1, db_path=initialized_db)
    rec2 = await vision_db.get_vision_image(100, 2, db_path=initialized_db)

    stats = await processor.attach_images_to_task(
        task_id=42,
        trello_card_id="card_42",
        image_records=[rec1, rec2],
        db_path=initialized_db,
    )
    assert stats["attached"] == 1
    assert stats["skipped_duplicate"] == 1
    assert mock_trello.attach_image_to_card.call_count == 1

    # Verify temp files were cleaned up and DB metadata persisted
    assert not os.path.exists(path1)
    assert not os.path.exists(path2)
    fresh1 = await vision_db.get_vision_image(100, 1, db_path=initialized_db)
    assert fresh1["attachment_status"] == "attached"
    assert fresh1["trello_attachment_id"] == "att_001"
    assert fresh1["local_file_path"] is None


@pytest.mark.asyncio
async def test_partial_failure_preserves_task_and_retries_successfully(initialized_db, mock_bot, tmp_path):
    """
    Task creation succeeds + Trello card creation succeeds + attachment fails ->
    task is NOT deleted, state is persisted as failed, and retry_pending_attachments succeeds later!
    """
    temp_dir = str(tmp_path / "vision_retry")
    path1, hash1 = _write_temp_jpeg(temp_dir, color=(200, 50, 50))

    task_id = await bot_module.create_task(
        title="Купить смеситель",
        trello_card_id="card_faucet",
        trello_card_url="https://trello.com/c/faucet",
        db_path=initialized_db,
    )
    await vision_db.create_vision_image(
        chat_id=100,
        telegram_message_id=50,
        sender_id=bot_module.WIFE_ID,
        telegram_file_id="tg_file_50",
        telegram_file_unique_id="tg_uniq_50",
        db_path=initialized_db,
    )
    await vision_db.update_vision_image(
        100,
        50,
        local_file_path=path1,
        original_filename="photo_50.jpg",
        mime_type="image/jpeg",
        sha256_hash=hash1,
        vision_status="completed",
        db_path=initialized_db,
    )

    mock_trello = MagicMock()
    mock_trello.is_enabled = True
    # First attempt fails (Trello attachment outage), retry succeeds
    mock_trello.attach_image_to_card = AsyncMock(side_effect=[None, {"id": "att_recovered_50"}])

    cfg = VisionConfig(
        temp_dir=temp_dir,
        keep_local_files=False,
        retry_max_attempts=3,
        retry_base_delay_seconds=0.0,
    )
    processor = VisionProcessor(
        config=cfg,
        analyzer=OllamaVisionAnalyzer(cfg),
        authorized_user_id=bot_module.WIFE_ID,
        owner_user_id=bot_module.MY_ID,
        save_chat_message_fn=AsyncMock(),
        get_recent_history_fn=AsyncMock(return_value=[]),
        reset_debounce_fn=MagicMock(),
        trello_service_fn=lambda: mock_trello,
        db_path_fn=lambda: initialized_db,
        get_task_by_id_fn=bot_module.get_task_by_id,
    )

    rec = await vision_db.get_vision_image(100, 50, db_path=initialized_db)
    stats = await processor.attach_images_to_task(
        task_id=task_id,
        trello_card_id="card_faucet",
        image_records=[rec],
        bot=mock_bot,
        db_path=initialized_db,
    )
    assert stats["failed"] == 1

    # Task is intact!
    task = await bot_module.get_task_by_id(task_id, db_path=initialized_db)
    assert task is not None
    assert task["title"] == "Купить смеситель"

    # Attachment state is recorded as failed and file retained for retry
    failed_rec = await vision_db.get_vision_image(100, 50, db_path=initialized_db)
    assert failed_rec["attachment_status"] == "failed"
    assert failed_rec["retry_count"] == 1
    assert os.path.exists(path1)

    # Background retry succeeds and cleans up temp file
    retry_stats = await processor.retry_pending_attachments(bot=mock_bot, db_path=initialized_db)
    assert retry_stats["checked"] == 1
    assert retry_stats["attached"] == 1

    ok_rec = await vision_db.get_vision_image(100, 50, db_path=initialized_db)
    assert ok_rec["attachment_status"] == "attached"
    assert ok_rec["trello_attachment_id"] == "att_recovered_50"
    assert not os.path.exists(path1)


@pytest.mark.asyncio
async def test_no_secrets_or_ocr_leaked_in_logs(initialized_db, tmp_path, caplog):
    temp_dir = str(tmp_path / "vision_logs")
    file_path, _ = _write_temp_jpeg(temp_dir)
    secret_token = "SUPER_SECRET_TRELLO_TOKEN_XYZ"
    secret_ocr = "CONFIDENTIAL_PASSPORT_NUMBER_999888"

    mock_session = _mock_aiohttp_session(status=500, json_data={"error": secret_token})
    client = TrelloClient(api_key="KEY_SECRET_123", token=secret_token, session=mock_session)

    with caplog.at_level(logging.DEBUG):
        await client.upload_card_attachment(card_id="card_sec", file_path=file_path)

    full_log = "\n".join(rec.getMessage() for rec in caplog.records)
    assert secret_token not in full_log
    assert "KEY_SECRET_123" not in full_log
    assert secret_ocr not in full_log
