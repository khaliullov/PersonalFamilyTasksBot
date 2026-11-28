"""Comprehensive integration tests for Telegram photo/album flows, context sequences, task pipeline, lifecycle, and disabled Vision."""

import asyncio
from datetime import datetime
import io
import os
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import Chat, Document, File, Message, PhotoSize, User, Voice
from PIL import Image
import pytest

import bot as bot_module
from vision import db as vision_db
from vision.analyzer import OllamaVisionAnalyzer
from vision.config import VisionConfig
from vision.exceptions import VisionUnavailableError
from vision.models import VisionAnalysisResult, VisionCategory
from vision.processor import VISION_UNAVAILABLE_MESSAGE, VisionProcessor


def _make_jpeg_bytes(color=(10, 120, 220), size=(160, 120)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


def _make_png_bytes(color=(50, 200, 80), size=(200, 140)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def _make_photo_message(
    *,
    message_id: int = 100,
    user_id: int = bot_module.WIFE_ID,
    chat_id: int = 12345,
    caption: str | None = None,
    media_group_id: str | None = None,
    file_size: int = 2048,
    file_unique_id: str | None = None,
    business_connection_id: str = "biz_1",
) -> Message:
    uid = file_unique_id or f"uniq_photo_{message_id}"
    return Message(
        message_id=message_id,
        date=datetime.now(),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Wife"),
        caption=caption,
        media_group_id=media_group_id,
        business_connection_id=business_connection_id,
        photo=[
            PhotoSize(
                file_id=f"small_{message_id}",
                file_unique_id=f"small_{uid}",
                width=90,
                height=60,
                file_size=512,
            ),
            PhotoSize(
                file_id=f"photo_{message_id}",
                file_unique_id=uid,
                width=800,
                height=600,
                file_size=file_size,
            ),
        ],
    )


def _make_image_doc_message(
    *,
    message_id: int = 150,
    user_id: int = bot_module.WIFE_ID,
    chat_id: int = 12345,
    caption: str | None = None,
    mime_type: str = "image/png",
    file_size: int = 4096,
) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Wife"),
        caption=caption,
        business_connection_id="biz_1",
        document=Document(
            file_id=f"doc_{message_id}",
            file_unique_id=f"uniq_doc_{message_id}",
            file_name="receipt.png",
            mime_type=mime_type,
            file_size=file_size,
        ),
    )


def _make_text_message(message_id: int, text: str, user_id: int = bot_module.WIFE_ID) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(),
        chat=Chat(id=12345, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Wife"),
        text=text,
        business_connection_id="biz_1",
    )


def _make_voice_message(message_id: int, user_id: int = bot_module.WIFE_ID) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(),
        chat=Chat(id=12345, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Wife"),
        voice=Voice(
            file_id=f"voice_{message_id}",
            file_unique_id=f"uniq_voice_{message_id}",
            duration=6,
            file_size=1024,
        ),
        business_connection_id="biz_1",
    )


def _configure_bot_for_images(mock_bot, payload_by_file_id: dict[str, bytes] | None = None, default_bytes: bytes | None = None):
    default_content = default_bytes if default_bytes is not None else _make_jpeg_bytes()
    payload_map = payload_by_file_id or {}

    async def fake_get_file(file_id):
        return File(
            file_id=file_id,
            file_unique_id=f"u_{file_id}",
            file_path=f"photos/{file_id}.jpg",
            file_size=len(payload_map.get(file_id, default_content)),
        )

    async def fake_download(file_path, destination):
        # Extract file_id from "photos/{file_id}.jpg"
        fid = os.path.basename(file_path).replace(".jpg", "")
        data = payload_map.get(fid, default_content)
        with open(destination, "wb") as fh:
            fh.write(data)
        return destination

    mock_bot.get_file = AsyncMock(side_effect=fake_get_file)
    mock_bot.download_file = AsyncMock(side_effect=fake_download)
    return mock_bot


def _build_processor(tmp_path, db_path, analyzer, *, trello_svc=None, enabled=True, album_wait=0.03, keep_local=False):
    cfg = VisionConfig(
        enabled=enabled,
        temp_dir=str(tmp_path / "vision_temp"),
        keep_local_files=keep_local,
        album_wait_seconds=album_wait,
        retry_max_attempts=2,
        retry_base_delay_seconds=0.0,
    )
    return VisionProcessor(
        config=cfg,
        analyzer=analyzer,
        authorized_user_id=bot_module.WIFE_ID,
        owner_user_id=bot_module.MY_ID,
        save_chat_message_fn=lambda **kw: bot_module.save_chat_message(**kw),
        get_recent_history_fn=lambda **kw: bot_module.get_recent_chat_history(**kw),
        reset_debounce_fn=lambda uid, b: bot_module.reset_debounce_timer(uid, b),
        trello_service_fn=lambda: trello_svc,
        db_path_fn=lambda: db_path,
        get_task_by_id_fn=bot_module.get_task_by_id,
    )


def _sample_result(**overrides) -> VisionAnalysisResult:
    base = {
        "image_description": "Бутылка стирального геля Persil Color 1.3 л",
        "short_summary": "Стиральный гель Persil Color 1.3 л",
        "visible_text": ["Persil", "Color", "1.3L"],
        "objects": ["гель для стирки"],
        "brands_and_models": ["Persil Color"],
        "measurements": ["1.3 л"],
        "condition_or_problem": None,
        "category": "shopping",
        "task_candidate": True,
        "suggested_title": "Купить гель Persil Color 1.3 л",
        "suggested_details": "Persil Color 1.3 л",
        "confidence": 0.92,
        "uncertainties": [],
        "requires_clarification": False,
        "clarification_question": None,
    }
    base.update(overrides)
    return VisionAnalysisResult.model_validate(base)


# ==============================================================================
# 1. Authorization & Security
# ==============================================================================


@pytest.mark.asyncio
async def test_unauthorized_photo_rejected_before_download(initialized_db, mock_bot, tmp_path):
    _configure_bot_for_images(mock_bot)
    analyzer = MagicMock(spec=OllamaVisionAnalyzer)
    analyzer.analyze_image = AsyncMock(return_value=_sample_result())
    processor = _build_processor(tmp_path, initialized_db, analyzer)

    stranger_photo = _make_photo_message(message_id=901, user_id=9999999, caption="Купи это")

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "vision_processor", processor):
        await bot_module.handle_wife_business_message(stranger_photo, mock_bot)
        await processor.wait_for_idle()

    mock_bot.get_file.assert_not_called()
    mock_bot.download_file.assert_not_called()
    analyzer.analyze_image.assert_not_called()
    assert await bot_module.get_recent_chat_history(db_path=initialized_db) == []


# ==============================================================================
# 2. Single Photo + Caption -> Task Created + Trello Card + Attachment
# ==============================================================================


@pytest.mark.asyncio
async def test_photo_with_caption_creates_task_and_attaches_to_trello(initialized_db, mock_bot, tmp_path):
    _configure_bot_for_images(mock_bot)
    analyzer = MagicMock(spec=OllamaVisionAnalyzer)
    analyzer.analyze_image = AsyncMock(return_value=_sample_result())

    mock_trello = MagicMock()
    mock_trello.is_enabled = True
    mock_trello.sync_task_created = AsyncMock(
        return_value={"card_id": "card_persil", "card_url": "https://trello.com/c/persil"}
    )
    mock_trello.attach_image_to_card = AsyncMock(return_value={"id": "att_persil"})

    processor = _build_processor(tmp_path, initialized_db, analyzer, trello_svc=mock_trello)

    llm_decision = {
        "action": "create",
        "is_task": True,
        "task_id": None,
        "task_title": "Купить гель Persil Color 1.3 л",
        "task_description": "Стиральный гель Persil Color 1.3 л",
        "has_deadline": False,
        "deadline_timestamp": None,
    }

    msg = _make_photo_message(message_id=201, caption="Купи такой")

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "DEBOUNCE_DELAY_SECONDS", 0.02), \
         patch.object(bot_module, "vision_processor", processor), \
         patch.object(bot_module, "trello_service", mock_trello), \
         patch("bot.analyze_context_with_ollama", new=AsyncMock(return_value=llm_decision)):

        await bot_module.handle_wife_business_message(msg, mock_bot)
        await processor.wait_for_idle()
        await asyncio.sleep(0.08)

    # 1. Task created via existing pipeline
    tasks = await bot_module.get_active_tasks(db_path=initialized_db)
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Купить гель Persil Color 1.3 л"
    assert tasks[0]["trello_card_id"] == "card_persil"

    # 2. Trello card description contains Markdown image section
    trello_desc = mock_trello.sync_task_created.call_args.kwargs["description"]
    assert "## Информация по фото" in trello_desc
    assert "Persil Color" in trello_desc

    # 3. Original photo attached to Trello card & temp file cleaned up
    mock_trello.attach_image_to_card.assert_called_once()
    assert mock_trello.attach_image_to_card.call_args.kwargs["card_id"] == "card_persil"

    rec = await vision_db.get_vision_image(12345, 201, db_path=initialized_db)
    assert rec["task_id"] == tasks[0]["id"]
    assert rec["attachment_status"] == "attached"
    assert rec["trello_attachment_id"] == "att_persil"
    assert rec["local_file_path"] is None

    # 4. Notification to husband mentions photo & attachment
    sent_text = mock_bot.send_message.call_args.kwargs["text"]
    assert "Описание по фото" in sent_text
    assert "Фото прикреплено к карточке Trello" in sent_text


# ==============================================================================
# 3. Text -> Photo Associates with Existing Task (No Duplicate Task!)
# ==============================================================================


@pytest.mark.asyncio
async def test_text_followed_by_photo_updates_existing_task_and_attaches(initialized_db, mock_bot, tmp_path):
    _configure_bot_for_images(mock_bot)
    faucet_result = _sample_result(
        image_description="Хромированный смеситель в ванной с течью у основания.",
        short_summary="Смеситель с течью у основания",
        visible_text=[],
        brands_and_models=[],
        category="repair",
    )
    analyzer = MagicMock(spec=OllamaVisionAnalyzer)
    analyzer.analyze_image = AsyncMock(return_value=faucet_result)

    mock_trello = MagicMock()
    mock_trello.is_enabled = True
    mock_trello.sync_task_created = AsyncMock(
        return_value={"card_id": "card_faucet_1", "card_url": "https://trello.com/c/faucet1"}
    )
    mock_trello.sync_task_updated = AsyncMock(return_value=True)
    mock_trello.attach_image_to_card = AsyncMock(return_value={"id": "att_faucet_1"})

    processor = _build_processor(tmp_path, initialized_db, analyzer, trello_svc=mock_trello)

    create_decision = {
        "action": "create",
        "is_task": True,
        "task_id": None,
        "task_title": "Заменить смеситель в ванной",
        "task_description": None,
        "has_deadline": False,
        "deadline_timestamp": None,
    }
    update_decision = {
        "action": "update",
        "is_task": True,
        "task_id": 1,
        "task_title": "Заменить смеситель в ванной",
        "task_description": "Хромированный смеситель с течью у основания",
        "has_deadline": False,
        "deadline_timestamp": None,
    }

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "DEBOUNCE_DELAY_SECONDS", 0.02), \
         patch.object(bot_module, "vision_processor", processor), \
         patch.object(bot_module, "trello_service", mock_trello), \
         patch("bot.analyze_context_with_ollama", new=AsyncMock(side_effect=[create_decision, update_decision])):

        # Step 1: Text message creates the task
        await bot_module.handle_wife_business_message(
            _make_text_message(301, "Нужно заменить смеситель в ванной."), mock_bot
        )
        await asyncio.sleep(0.08)

        # Step 2: Photo without caption follows -> attaches to existing task #1
        await bot_module.handle_wife_business_message(
            _make_photo_message(message_id=302, caption=None), mock_bot
        )
        await processor.wait_for_idle()
        await asyncio.sleep(0.08)

    # Only ONE task exists — no duplicate!
    tasks = await bot_module.get_active_tasks(db_path=initialized_db)
    assert len(tasks) == 1
    assert tasks[0]["id"] == 1
    assert "смеситель" in tasks[0]["title"].lower()

    # Trello card was updated and photo was attached to existing card_faucet_1
    mock_trello.sync_task_updated.assert_called_once()
    mock_trello.attach_image_to_card.assert_called_once()
    assert mock_trello.attach_image_to_card.call_args.kwargs["card_id"] == "card_faucet_1"


# ==============================================================================
# 4. Album / Media Group Ordering & Single Logical Event
# ==============================================================================


@pytest.mark.asyncio
async def test_album_processed_as_single_event_and_all_photos_attached(initialized_db, mock_bot, tmp_path):
    # Distinct images so SHA-256 hashes differ
    _configure_bot_for_images(
        mock_bot,
        payload_by_file_id={
            "photo_401": _make_jpeg_bytes(color=(255, 0, 0)),
            "photo_402": _make_jpeg_bytes(color=(0, 255, 0)),
            "photo_403": _make_jpeg_bytes(color=(0, 0, 255)),
        },
    )
    analyzer = MagicMock(spec=OllamaVisionAnalyzer)
    analyzer.analyze_image = AsyncMock(
        side_effect=[
            _sample_result(image_description="Фото 1: фасад шкафа", short_summary="Фасад шкафа"),
            _sample_result(image_description="Фото 2: сломанная петля", short_summary="Сломанная петля"),
            _sample_result(image_description="Фото 3: маркировка петли Blum", short_summary="Петля Blum"),
        ]
    )

    mock_trello = MagicMock()
    mock_trello.is_enabled = True
    mock_trello.sync_task_created = AsyncMock(
        return_value={"card_id": "card_album", "card_url": "https://trello.com/c/album"}
    )
    mock_trello.attach_image_to_card = AsyncMock(
        side_effect=[{"id": "att_1"}, {"id": "att_2"}, {"id": "att_3"}]
    )

    processor = _build_processor(tmp_path, initialized_db, analyzer, trello_svc=mock_trello, album_wait=0.04)

    llm_decision = {
        "action": "create",
        "is_task": True,
        "task_id": None,
        "task_title": "Починить петлю шкафа Blum",
        "task_description": "Сломана петля на фасаде шкафа",
        "has_deadline": False,
        "deadline_timestamp": None,
    }

    # Arrive out of order (403, 401, 402) to verify sorting by message_id!
    m3 = _make_photo_message(message_id=403, media_group_id="grp_99", caption=None)
    m1 = _make_photo_message(message_id=401, media_group_id="grp_99", caption="Посмотри, петля отвалилась")
    m2 = _make_photo_message(message_id=402, media_group_id="grp_99", caption=None)

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "DEBOUNCE_DELAY_SECONDS", 0.02), \
         patch.object(bot_module, "vision_processor", processor), \
         patch.object(bot_module, "trello_service", mock_trello), \
         patch("bot.analyze_context_with_ollama", new=AsyncMock(return_value=llm_decision)) as mock_llm:

        await bot_module.handle_wife_business_message(m3, mock_bot)
        await bot_module.handle_wife_business_message(m1, mock_bot)
        await bot_module.handle_wife_business_message(m2, mock_bot)
        await processor.wait_for_idle()
        await asyncio.sleep(0.1)

    # Single context entry in chat_history for the entire 3-photo album
    history = await bot_module.get_recent_chat_history(limit=10, db_path=initialized_db)
    assert len(history) == 1
    assert history[0]["message_type"] == "album"
    assert "альбом из 3 изображений" in history[0]["text"]
    assert "Посмотри, петля отвалилась" in history[0]["text"]
    # Verify ordering: Photo 1 before Photo 2 before Photo 3
    assert history[0]["text"].index("Фото 1") < history[0]["text"].index("Фото 2") < history[0]["text"].index("Фото 3")

    # LLM called ONCE, ONE task created, ONE notification sent, ALL 3 photos attached
    assert mock_llm.call_count == 1
    tasks = await bot_module.get_active_tasks(db_path=initialized_db)
    assert len(tasks) == 1
    assert mock_trello.attach_image_to_card.call_count == 3
    assert mock_bot.send_message.call_count == 1


# ==============================================================================
# 5. Photo -> Text, Voice -> Photo, Photo -> Voice Context Sequences
# ==============================================================================


@pytest.mark.asyncio
async def test_photo_then_text_creates_task_with_earlier_photo_attached(initialized_db, mock_bot, tmp_path):
    _configure_bot_for_images(mock_bot)
    analyzer = MagicMock(spec=OllamaVisionAnalyzer)
    analyzer.analyze_image = AsyncMock(return_value=_sample_result(task_candidate=False))

    mock_trello = MagicMock()
    mock_trello.is_enabled = True
    mock_trello.sync_task_created = AsyncMock(
        return_value={"card_id": "card_pt", "card_url": "https://trello.com/c/pt"}
    )
    mock_trello.attach_image_to_card = AsyncMock(return_value={"id": "att_pt"})

    processor = _build_processor(tmp_path, initialized_db, analyzer, trello_svc=mock_trello)

    none_decision = {"action": "none", "is_task": False}
    create_decision = {
        "action": "create",
        "is_task": True,
        "task_id": None,
        "task_title": "Купить гель Persil",
        "task_description": "По фото ранее",
        "has_deadline": False,
        "deadline_timestamp": None,
    }

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "DEBOUNCE_DELAY_SECONDS", 0.02), \
         patch.object(bot_module, "vision_processor", processor), \
         patch.object(bot_module, "trello_service", mock_trello), \
         patch("bot.analyze_context_with_ollama", new=AsyncMock(side_effect=[none_decision, create_decision])):

        await bot_module.handle_wife_business_message(_make_photo_message(message_id=501), mock_bot)
        await processor.wait_for_idle()
        await asyncio.sleep(0.08)

        # No task yet!
        assert len(await bot_module.get_active_tasks(db_path=initialized_db)) == 0

        # Follow-up text triggers task creation & attaches earlier unattached photo
        await bot_module.handle_wife_business_message(_make_text_message(502, "Купи такой вечером"), mock_bot)
        await asyncio.sleep(0.08)

    tasks = await bot_module.get_active_tasks(db_path=initialized_db)
    assert len(tasks) == 1
    mock_trello.attach_image_to_card.assert_called_once()


@pytest.mark.asyncio
async def test_voice_and_photo_preserve_chronological_context(initialized_db, mock_bot, tmp_path):
    _configure_bot_for_images(mock_bot)
    analyzer = MagicMock(spec=OllamaVisionAnalyzer)
    analyzer.analyze_image = AsyncMock(return_value=_sample_result(task_candidate=False))
    processor = _build_processor(tmp_path, initialized_db, analyzer)

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "DEBOUNCE_DELAY_SECONDS", 10.0), \
         patch.object(bot_module, "vision_processor", processor), \
         patch("voice.processor.transcribe_voice", new=AsyncMock(return_value="Вот фото квитанции")), \
         patch("voice.processor.generate_voice_summary", new=AsyncMock(return_value={"summary": "Квитанция", "category": "household", "key_points": []})):

        # Voice -> Photo -> Voice
        await bot_module.handle_wife_business_message(_make_voice_message(601), mock_bot)
        await bot_module.handle_wife_business_message(_make_photo_message(message_id=602), mock_bot)
        await processor.wait_for_idle()
        await bot_module.handle_wife_business_message(_make_voice_message(603), mock_bot)

    # Cancel debounce timers from test
    for t in bot_module._debounce_tasks.values():
        t.cancel()

    history = await bot_module.get_recent_chat_history(limit=10, db_path=initialized_db)
    assert [h["message_type"] for h in history] == ["voice", "image", "voice"]


# ==============================================================================
# 6. Irrelevant Photo / Receipt, Ambiguous Clarification, Duplicate Update, Document Image
# ==============================================================================


@pytest.mark.asyncio
async def test_receipt_without_task_context_does_not_create_task_and_image_doc_supported(
    initialized_db, mock_bot, tmp_path
):
    _configure_bot_for_images(mock_bot, default_bytes=_make_png_bytes())
    receipt_res = _sample_result(
        image_description="Кассовый чек из магазина Пятерочка на сумму 1450 руб.",
        short_summary="Чек из Пятерочки на 1450 руб.",
        visible_text=["Пятерочка", "ИТОГ 1450.00", "2026-11-28"],
        category="receipt",
        task_candidate=False,
        requires_clarification=False,
    )
    analyzer = MagicMock(spec=OllamaVisionAnalyzer)
    analyzer.analyze_image = AsyncMock(return_value=receipt_res)
    processor = _build_processor(tmp_path, initialized_db, analyzer)

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "DEBOUNCE_DELAY_SECONDS", 0.02), \
         patch.object(bot_module, "vision_processor", processor), \
         patch("bot.analyze_context_with_ollama", new=AsyncMock(return_value={"action": "none", "is_task": False})):

        await bot_module.handle_wife_business_message(_make_image_doc_message(message_id=701), mock_bot)
        await processor.wait_for_idle()
        await asyncio.sleep(0.08)

    assert len(await bot_module.get_active_tasks(db_path=initialized_db)) == 0
    rec = await vision_db.get_vision_image(12345, 701, db_path=initialized_db)
    assert rec["category"] == "receipt"
    assert "Пятерочка" in rec["ocr_text"]
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_ambiguous_photo_sends_single_clarification_question(initialized_db, mock_bot, tmp_path):
    _configure_bot_for_images(mock_bot)
    ambiguous = _sample_result(
        image_description="Размытое фото детали от бытовой техники.",
        short_summary="Деталь от бытовой техники",
        category="other",
        task_candidate=False,
        confidence=0.4,
        uncertainties=["Деталь неразборчива"],
        requires_clarification=True,
        clarification_question="Это нужно добавить в существующую задачу или создать новую?",
    )
    analyzer = MagicMock(spec=OllamaVisionAnalyzer)
    analyzer.analyze_image = AsyncMock(return_value=ambiguous)
    processor = _build_processor(tmp_path, initialized_db, analyzer)

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "DEBOUNCE_DELAY_SECONDS", 0.02), \
         patch.object(bot_module, "vision_processor", processor), \
         patch("bot.analyze_context_with_ollama", new=AsyncMock(return_value={"action": "none", "is_task": False})):

        await bot_module.handle_wife_business_message(_make_photo_message(message_id=710), mock_bot)
        await processor.wait_for_idle()
        await asyncio.sleep(0.08)

        # Calling context analysis again must NOT re-send clarification
        await bot_module.process_recent_context_with_ollama(mock_bot, bot_module.WIFE_ID)

    assert mock_bot.send_message.call_count == 1
    sent = mock_bot.send_message.call_args.kwargs["text"]
    assert "существующую задачу или создать новую" in sent


@pytest.mark.asyncio
async def test_duplicate_telegram_update_is_idempotent(initialized_db, mock_bot, tmp_path):
    _configure_bot_for_images(mock_bot)
    analyzer = MagicMock(spec=OllamaVisionAnalyzer)
    analyzer.analyze_image = AsyncMock(return_value=_sample_result(task_candidate=False))
    processor = _build_processor(tmp_path, initialized_db, analyzer)

    msg = _make_photo_message(message_id=720)
    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "DEBOUNCE_DELAY_SECONDS", 10.0), \
         patch.object(bot_module, "vision_processor", processor):

        await processor.process_photo_batch([msg], mock_bot, db_path=initialized_db)
        await processor.process_photo_batch([msg], mock_bot, db_path=initialized_db)

    for t in bot_module._debounce_tasks.values():
        t.cancel()

    assert analyzer.analyze_image.call_count == 1
    history = await bot_module.get_recent_chat_history(limit=10, db_path=initialized_db)
    assert len(history) == 1


# ==============================================================================
# 7. Failure Handling, Cancellation, Restart Recovery, and Vision Disabled Mode
# ==============================================================================


@pytest.mark.asyncio
async def test_vision_outage_preserves_caption_and_notifies_gracefully(initialized_db, mock_bot, tmp_path):
    _configure_bot_for_images(mock_bot)
    analyzer = MagicMock(spec=OllamaVisionAnalyzer)
    analyzer.analyze_image = AsyncMock(side_effect=VisionUnavailableError("Ollama down"))
    processor = _build_processor(tmp_path, initialized_db, analyzer)

    msg = _make_photo_message(message_id=801, caption="Купи это завтра")
    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "DEBOUNCE_DELAY_SECONDS", 10.0), \
         patch.object(bot_module, "vision_processor", processor):

        await processor.process_photo_batch([msg], mock_bot, db_path=initialized_db)

    for t in bot_module._debounce_tasks.values():
        t.cancel()

    # Fallback message sent to owner
    mock_bot.send_message.assert_called_once()
    assert mock_bot.send_message.call_args.kwargs["text"] == VISION_UNAVAILABLE_MESSAGE

    # Caption was STILL preserved in chat_history so text pipeline can work!
    history = await bot_module.get_recent_chat_history(limit=5, db_path=initialized_db)
    assert len(history) == 1
    assert "Купи это завтра" in history[0]["text"]

    # Temp file cleaned up on failure
    assert os.listdir(processor.config.temp_dir) == []


@pytest.mark.asyncio
async def test_download_failure_and_corrupted_image_and_size_limit(initialized_db, mock_bot, tmp_path):
    analyzer = MagicMock(spec=OllamaVisionAnalyzer)
    analyzer.analyze_image = AsyncMock(return_value=_sample_result())
    processor = _build_processor(tmp_path, initialized_db, analyzer)

    # 1. File size exceeded -> rejected before download
    huge_msg = _make_photo_message(message_id=810, file_size=25 * 1024 * 1024)
    mock_bot.get_file = AsyncMock()
    await processor.process_photo_batch([huge_msg], mock_bot, db_path=initialized_db)
    mock_bot.get_file.assert_not_called()
    rec_huge = await vision_db.get_vision_image(12345, 810, db_path=initialized_db)
    assert rec_huge["vision_status"] == "failed"

    # 2. Download failure
    mock_bot.get_file = AsyncMock(side_effect=RuntimeError("Telegram network error"))
    await processor.process_photo_batch([_make_photo_message(message_id=811)], mock_bot, db_path=initialized_db)
    rec_dl = await vision_db.get_vision_image(12345, 811, db_path=initialized_db)
    assert rec_dl["vision_status"] == "failed"
    assert "VisionDownloadError" in rec_dl["last_error"]

    # 3. Corrupted image bytes
    _configure_bot_for_images(mock_bot, default_bytes=b"NOT_A_REAL_IMAGE_PAYLOAD")
    await processor.process_photo_batch([_make_photo_message(message_id=812)], mock_bot, db_path=initialized_db)
    rec_bad = await vision_db.get_vision_image(12345, 812, db_path=initialized_db)
    assert rec_bad["vision_status"] == "failed"
    assert "VisionImageError" in rec_bad["last_error"]
    assert os.listdir(processor.config.temp_dir) == []


@pytest.mark.asyncio
async def test_cancellation_cleans_up_temp_file_and_restart_recovery(initialized_db, mock_bot, tmp_path):
    _configure_bot_for_images(mock_bot)
    analyzer = MagicMock(spec=OllamaVisionAnalyzer)
    analyzer.analyze_image = AsyncMock(side_effect=asyncio.CancelledError())
    processor = _build_processor(tmp_path, initialized_db, analyzer)

    with pytest.raises(asyncio.CancelledError):
        await processor.process_photo_batch([_make_photo_message(message_id=820)], mock_bot, db_path=initialized_db)

    # Temp file removed on cancellation
    assert os.listdir(processor.config.temp_dir) == []
    rec = await vision_db.get_vision_image(12345, 820, db_path=initialized_db)
    assert rec["vision_status"] == "failed"
    assert rec["last_error"] == "CancelledError"

    # Simulate an interrupted record from a bot crash/restart
    orphan = os.path.join(processor.config.temp_dir, "vision_orphan.jpg")
    with open(orphan, "wb") as fh:
        fh.write(b"stale")
    await vision_db.create_vision_image(
        chat_id=12345,
        telegram_message_id=821,
        sender_id=bot_module.WIFE_ID,
        db_path=initialized_db,
    )
    await vision_db.update_vision_image(
        12345,
        821,
        vision_status="analyzing",
        local_file_path=orphan,
        db_path=initialized_db,
    )

    recovery = await processor.recover_on_startup(db_path=initialized_db)
    assert recovery["recovered_interrupted"] == 1
    assert not os.path.exists(orphan)
    rec2 = await vision_db.get_vision_image(12345, 821, db_path=initialized_db)
    assert rec2["vision_status"] == "failed"
    assert rec2["last_error"] == "InterruptedByRestart"


@pytest.mark.asyncio
async def test_vision_disabled_preserves_caption_text_pipeline(initialized_db, mock_bot, tmp_path):
    _configure_bot_for_images(mock_bot)
    analyzer = MagicMock(spec=OllamaVisionAnalyzer)
    analyzer.analyze_image = AsyncMock(return_value=_sample_result())
    disabled_processor = _build_processor(tmp_path, initialized_db, analyzer, enabled=False)

    llm_decision = {
        "action": "create",
        "is_task": True,
        "task_id": None,
        "task_title": "Купить молоко",
        "task_description": None,
        "has_deadline": False,
        "deadline_timestamp": None,
    }

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "DEBOUNCE_DELAY_SECONDS", 0.02), \
         patch.object(bot_module, "vision_processor", disabled_processor), \
         patch.object(bot_module, "trello_service", None), \
         patch("bot.analyze_context_with_ollama", new=AsyncMock(return_value=llm_decision)):

        # Photo with caption -> falls back to caption-as-text!
        await bot_module.handle_wife_business_message(
            _make_photo_message(message_id=830, caption="Купи молоко"), mock_bot
        )
        await asyncio.sleep(0.08)

    mock_bot.get_file.assert_not_called()
    analyzer.analyze_image.assert_not_called()
    tasks = await bot_module.get_active_tasks(db_path=initialized_db)
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Купить молоко"
