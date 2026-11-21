"""
Тесты для новых возможностей:
1. Часовой пояс UTC+4 и настраиваемость через конфигурацию.
2. Завершение задачи по inline кнопке в Telegram.
3. Перемещение задачи в Done в Trello при закрытии из Telegram.
4. Распознавание категории 'calendar', извлечение локации, контактов, ссылок и синхронизация с Apple Calendar (CalDAV).
5. Двусторонняя синхронизация изменения топика/дедлайна из Trello обратно в бота.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
import zoneinfo

import pytest

import bot as bot_module
from calendar_sync.service import CalendarSyncService
from trello.client import TrelloClient
from trello.service import TrelloSyncService
import tz_utils


# ==============================================================================
# 1. ТЕСТЫ ЧАСОВОГО ПОЯСА
# ==============================================================================


def test_timezone_parser_formats():
    """Проверяет парсинг различных форматов строкового представления часового пояса."""
    # По умолчанию UTC+4
    assert tz_utils.parse_timezone("") == timezone(timedelta(hours=4))
    assert tz_utils.parse_timezone(None) == timezone(timedelta(hours=4))
    assert tz_utils.parse_timezone("invalid_timezone_123") == timezone(timedelta(hours=4))

    # Смещения UTC
    tz_plus4 = tz_utils.parse_timezone("UTC+4")
    assert tz_plus4 == timezone(timedelta(hours=4))

    tz_plus4_long = tz_utils.parse_timezone("UTC+04:00")
    assert tz_plus4_long == timezone(timedelta(hours=4))

    tz_bare = tz_utils.parse_timezone("+04:00")
    assert tz_bare == timezone(timedelta(hours=4))

    tz_short = tz_utils.parse_timezone("+4")
    assert tz_short == timezone(timedelta(hours=4))

    tz_minus3 = tz_utils.parse_timezone("UTC-3")
    assert tz_minus3 == timezone(timedelta(hours=-3))

    # IANA зоны
    tz_dubai = tz_utils.parse_timezone("Asia/Dubai")
    assert isinstance(tz_dubai, (timezone, zoneinfo.ZoneInfo))


def test_parse_deadline_attaches_bot_timezone():
    """Проверяет, что parse_deadline возвращает aware datetime с часовым поясом бота."""
    dt = bot_module.parse_deadline("2026-11-25 09:00:00")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.year == 2026
    assert dt.month == 11
    assert dt.day == 25
    assert dt.hour == 9
    assert dt.minute == 0


def test_parse_deadline_iso_with_z_converts_to_bot_timezone():
    """Проверяет, что ISO строки с Z (UTC) конвертируются в BOT_TIMEZONE."""
    # 05:00 UTC при BOT_TIMEZONE UTC+4 должно стать 09:00 UTC+4
    dt = bot_module.parse_deadline("2026-11-25T05:00:00Z")
    assert dt is not None
    assert dt.hour == 9


# ==============================================================================
# 2. ТЕСТЫ INLINE КНОПОК ЗАВЕРШЕНИЯ В TELEGRAM
# ==============================================================================


def test_get_task_inline_keyboard():
    """Проверяет корректность разметки inline клавиатуры для задачи."""
    kb = bot_module.get_task_inline_keyboard(42)
    assert kb is not None
    assert len(kb.inline_keyboard) == 1
    assert len(kb.inline_keyboard[0]) == 1
    button = kb.inline_keyboard[0][0]
    assert button.callback_data == "done:42"
    assert "Завершить" in button.text


@pytest.mark.asyncio
async def test_callback_done_task_completes_task(initialized_db):
    """Проверяет, что нажатие на inline кнопку закрывает задачу в БД и Trello."""
    task_id = await bot_module.create_task(
        title="Купить витамины",
        db_path=initialized_db,
    )
    await bot_module.update_task_trello_info(
        task_id=task_id,
        trello_card_id="card_vit_123",
        trello_card_url="https://trello.com/c/vit",
        db_path=initialized_db,
    )

    mock_client = AsyncMock(spec=TrelloClient)
    mock_trello_service = AsyncMock(spec=TrelloSyncService)
    mock_trello_service.is_enabled = True
    mock_trello_service.done_list_ids = {"done_list_id_1"}
    mock_trello_service.client = mock_client

    callback = AsyncMock()
    callback.data = f"done:{task_id}"
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "trello_service", mock_trello_service):

        await bot_module.callback_done_task(callback)

    # 1. Проверяем статус в SQLite
    task = await bot_module.get_task_by_id(task_id, db_path=initialized_db)
    assert task["status"] == "completed"

    # 2. Проверяем вызов Trello update_card
    mock_client.update_card.assert_called_once_with(
        card_id="card_vit_123",
        list_id="done_list_id_1",
    )

    # 3. Проверяем уведомление callback.answer и обновление разметки кнопки
    callback.answer.assert_called_once()
    assert f"#{task_id} выполнена" in callback.answer.call_args.args[0]
    callback.message.edit_reply_markup.assert_called_once()


@pytest.mark.asyncio
async def test_callback_done_task_already_completed(initialized_db):
    """Проверяет обработку нажатия на кнопку уже завершенной задачи."""
    task_id = await bot_module.create_task(
        title="Уже сделано",
        db_path=initialized_db,
    )
    await bot_module.update_task_status(task_id, "completed", db_path=initialized_db)

    callback = AsyncMock()
    callback.data = f"done:{task_id}"
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    with patch.object(bot_module, "DB_PATH", initialized_db):
        await bot_module.callback_done_task(callback)

    callback.answer.assert_called_once()
    assert "уже выполнена" in callback.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_cmd_tasks_includes_inline_buttons(initialized_db):
    """Проверяет, что команда /tasks возвращает сообщение с inline клавиатурой."""
    t1 = await bot_module.create_task(title="Задача 1", db_path=initialized_db)
    t2 = await bot_module.create_task(title="Задача 2", db_path=initialized_db)

    fake_message = AsyncMock()
    fake_message.answer = AsyncMock()

    with patch.object(bot_module, "DB_PATH", initialized_db):
        await bot_module.cmd_tasks(fake_message)

    fake_message.answer.assert_called_once()
    reply_markup = fake_message.answer.call_args.kwargs.get("reply_markup")
    assert reply_markup is not None
    assert len(reply_markup.inline_keyboard) == 2
    assert reply_markup.inline_keyboard[0][0].callback_data == f"done:{t2}"
    assert reply_markup.inline_keyboard[1][0].callback_data == f"done:{t1}"


# ==============================================================================
# 3. ТЕСТЫ СИНХРОНИЗАЦИИ TRELLO В DONE ПРИ ЗАКРЫТИИ ИЗ TELEGRAM
# ==============================================================================


@pytest.mark.asyncio
async def test_complete_task_by_id_calls_trello(initialized_db):
    """Проверяет функцию complete_task_by_id при наличии Trello."""
    task_id = await bot_module.create_task(
        title="Сдать отчет",
        db_path=initialized_db,
    )
    card_id = "card_report_777"
    await bot_module.update_task_trello_info(
        task_id=task_id,
        trello_card_id=card_id,
        trello_card_url="https://trello.com/c/report",
        db_path=initialized_db,
    )

    mock_client = AsyncMock(spec=TrelloClient)
    mock_trello = AsyncMock(spec=TrelloSyncService)
    mock_trello.is_enabled = True
    mock_trello.done_list_ids = {"done_col_99"}
    mock_trello.client = mock_client

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "trello_service", mock_trello):

        await bot_module.complete_task_by_id(task_id, db_path=initialized_db)

    task = await bot_module.get_task_by_id(task_id, db_path=initialized_db)
    assert task["status"] == "completed"
    mock_client.update_card.assert_called_once_with(
        card_id=card_id,
        list_id="done_col_99",
    )


# ==============================================================================
# 4. ТЕСТЫ КАЛЕНДАРЯ (КАТЕГОРИЯ calendar, CalDAV, ПОЛЯ location/meeting_url/contact_info)
# ==============================================================================


def test_extract_and_parse_json_calendar_fields():
    """Проверяет, что extract_and_parse_json сохраняет поля категории calendar."""
    raw = """
    ```json
    {
      "action": "create",
      "is_task": true,
      "category": "calendar",
      "task_title": "Идем к остеопату",
      "task_description": "Запись подтверждена",
      "has_deadline": true,
      "deadline_timestamp": "2026-11-25 09:00:00",
      "location": "ул. Пушкина 10",
      "meeting_url": null,
      "contact_info": "+79991234567"
    }
    ```
    """
    res = bot_module.extract_and_parse_json(raw)
    assert res["category"] == "calendar"
    assert res["task_title"] == "Идем к остеопату"
    assert res["location"] == "ул. Пушкина 10"
    assert res["contact_info"] == "+79991234567"
    assert res["meeting_url"] is None


@pytest.mark.asyncio
async def test_calendar_sync_service_create_event():
    """Проверяет работу CalendarSyncService с моком CalDAV."""
    mock_cal = MagicMock()
    mock_saved = MagicMock()
    mock_saved.url = "https://caldav.icloud.com/events/event123.ics"
    mock_cal.save_event.return_value = mock_saved
    mock_cal.name = "Personal"

    mock_principal = MagicMock()
    mock_principal.calendars.return_value = [mock_cal]

    mock_dav_client = MagicMock()
    mock_dav_client.principal.return_value = mock_principal

    service = CalendarSyncService(
        url="https://caldav.icloud.com/",
        username="user@icloud.com",
        password="secret-password",
        calendar_name="Personal",
        enabled=True,
    )
    assert service.is_enabled is True

    start_dt = tz_utils.get_now() + timedelta(days=2)
    with patch("caldav.DAVClient", return_value=mock_dav_client):
        res = await service.create_event(
            title="Прием у стоматолога",
            start_dt=start_dt,
            location="Клиника СтомАрт",
            meeting_url="https://zoom.us/j/123",
            contact_info="Врач Петров",
            description="Кабинет 3",
        )

    assert res is not None
    assert "uid" in res
    assert res["url"] == "https://caldav.icloud.com/events/event123.ics"
    mock_cal.save_event.assert_called_once()
    ical_payload = mock_cal.save_event.call_args[0][0]
    assert "Прием у стоматолога" in ical_payload
    assert "Клиника СтомАрт" in ical_payload
    assert "https://zoom.us/j/123" in ical_payload


@pytest.mark.asyncio
async def test_db_stores_and_retrieves_calendar_fields(initialized_db):
    """Проверяет сохранение и извлечение новых колонок задач в SQLite."""
    task_id = await bot_module.create_task(
        title="Консультация с юристом",
        description="Вопросы по договору",
        deadline="2026-11-26 14:00:00",
        category="calendar",
        location="Офис 15, Деловой центр",
        meeting_url="https://telemost.yandex.ru/123",
        contact_info="Юрист Анна +79110001122",
        caldav_uid="uid_cal_123",
        caldav_url="https://caldav.icloud.com/event.ics",
        db_path=initialized_db,
    )

    task = await bot_module.get_task_by_id(task_id, db_path=initialized_db)
    assert task is not None
    assert task["category"] == "calendar"
    assert task["location"] == "Офис 15, Деловой центр"
    assert task["meeting_url"] == "https://telemost.yandex.ru/123"
    assert task["contact_info"] == "Юрист Анна +79110001122"
    assert task["caldav_uid"] == "uid_cal_123"
    assert task["caldav_url"] == "https://caldav.icloud.com/event.ics"


# ==============================================================================
# 5. ТЕСТЫ ОБРАТНОЙ СИНХРОНИЗАЦИИ ИЗ TRELLO В БАЗУ И НАПОМИНАНИЯ
# ==============================================================================


@pytest.mark.asyncio
async def test_handle_trello_card_updated_syncs_title_and_deadline(initialized_db):
    """Проверяет обновление названия и дедлайна задачи при изменении в Trello."""
    task_id = await bot_module.create_task(
        title="Купить что-то странное (ошибка ИИ)",
        deadline="2026-11-25 10:00:00",
        db_path=initialized_db,
    )
    card_id = "card_edited_trello_001"
    await bot_module.update_task_trello_info(
        task_id=task_id,
        trello_card_id=card_id,
        trello_card_url="https://trello.com/c/edited",
        db_path=initialized_db,
    )

    # Имитируем вызов обратной синхронизации при переименовании карточки в Trello
    new_card_title = "Купить ортопедическую подушку ребенку"
    new_due = "2026-11-27T08:00:00Z"  # 12:00 UTC+4

    await bot_module.handle_trello_card_updated(
        card_id=card_id,
        card_title=new_card_title,
        card_desc="Уточнено в Trello",
        card_due=new_due,
        db_path=initialized_db,
    )

    # Проверяем, что задача в SQLite получила новое название и дедлайн
    task = await bot_module.get_task_by_id(task_id, db_path=initialized_db)
    assert task["title"] == new_card_title
    assert task["deadline"] == "2026-11-27 12:00:00"


@pytest.mark.asyncio
async def test_send_task_reminder_uses_latest_title_from_db(initialized_db):
    """
    Проверяет, что send_task_reminder берет актуальное название из БД,
    даже если при планировании было передано старое название!
    """
    task_id = await bot_module.create_task(
        title="Актуальное название после правок в Trello",
        deadline="2026-11-25 18:00:00",
        db_path=initialized_db,
    )

    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock()

    # Передаем в send_task_reminder старое ошибочное название 'Старое название ИИ'
    await bot_module.send_task_reminder(
        bot=mock_bot,
        task_id=task_id,
        title="Старое название ИИ",
        deadline_str="2026-11-25 18:00:00",
        my_id=12345,
        db_path=initialized_db,
    )

    mock_bot.send_message.assert_called_once()
    msg_text = mock_bot.send_message.call_args.kwargs.get("text", "")
    assert "Актуальное название после правок в Trello" in msg_text
    assert "Старое название ИИ" not in msg_text

    # Также проверяем наличие inline клавиатуры
    reply_markup = mock_bot.send_message.call_args.kwargs.get("reply_markup")
    assert reply_markup is not None
    assert reply_markup.inline_keyboard[0][0].callback_data == f"done:{task_id}"


@pytest.mark.asyncio
async def test_trello_service_poll_active_cards():
    """Проверяет опрос активных открытых карточек в poll_active_cards."""
    updated_cards = []

    async def on_card_updated(card_id: str, card_name: str, desc: str, due: str):
        updated_cards.append((card_id, card_name))

    mock_client = AsyncMock(spec=TrelloClient)
    mock_client.get_cards_on_board.return_value = [
        {"id": "c1", "name": "Карточка 1", "idList": "inbox_list", "desc": "", "due": None},
        {"id": "c2", "name": "Карточка 2", "idList": "done_list", "desc": "", "due": None},
    ]

    service = TrelloSyncService(
        api_key="key",
        token="token",
        board_id="board_123",
        client=mock_client,
    )
    service.done_list_ids = {"done_list"}
    service._on_card_updated_callback = on_card_updated

    with patch.object(TrelloSyncService, "is_enabled", new_callable=PropertyMock, return_value=True):
        await service.poll_active_cards()

    # Карточка 1 (не в Done) должна передаться в колбэк
    assert len(updated_cards) == 1
    assert updated_cards[0] == ("c1", "Карточка 1")


@pytest.mark.asyncio
async def test_trello_webhook_update_card_triggers_on_card_updated():
    """Проверяет обработку вебхука updateCard для активной карточки."""
    updated_cards = []

    async def on_card_updated(card_id: str, card_name: str, desc: str, due: str):
        updated_cards.append((card_id, card_name))

    service = TrelloSyncService(
        api_key="key",
        token="token",
        board_id="board_123",
    )
    service.done_list_ids = {"done_id"}
    service._on_card_updated_callback = on_card_updated

    payload = {
        "action": {
            "type": "updateCard",
            "data": {
                "card": {
                    "id": "card_updated_wh",
                    "name": "Новый заголовок",
                    "desc": "новые детали",
                },
                "old": {
                    "name": "Старый заголовок",
                },
            },
        }
    }

    await service.handle_webhook_payload(payload)
    assert len(updated_cards) == 1
    assert updated_cards[0] == ("card_updated_wh", "Новый заголовок")
