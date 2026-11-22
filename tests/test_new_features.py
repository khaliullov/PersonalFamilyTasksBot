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
from aiogram import types

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
    """Проверяет корректность разметки inline клавиатуры для задачи (кнопки Завершить и В календарь)."""
    # По умолчанию (с дедлайном): есть обе кнопки: [✅ Завершить] и [📅 В календарь]
    kb = bot_module.get_task_inline_keyboard(42)
    assert kb is not None
    assert len(kb.inline_keyboard) == 1
    assert len(kb.inline_keyboard[0]) == 2
    b1 = kb.inline_keyboard[0][0]
    b2 = kb.inline_keyboard[0][1]
    assert b1.callback_data == "done:42"
    assert "Завершить" in b1.text
    assert b2.callback_data == "to_calendar:42"
    assert "В календарь" in b2.text

    # Без явного дедлайна (has_deadline=False): кнопка "В календарь" НЕ добавляется!
    kb_no_dl = bot_module.get_task_inline_keyboard(42, has_deadline=False)
    assert len(kb_no_dl.inline_keyboard[0]) == 1
    assert kb_no_dl.inline_keyboard[0][0].callback_data == "done:42"

    # Если уже в календаре
    kb_cal = bot_module.get_task_inline_keyboard(42, show_calendar_button=False, is_in_calendar=True)
    assert len(kb_cal.inline_keyboard[0]) == 2
    assert kb_cal.inline_keyboard[0][1].callback_data == "noop_cal"
    assert "В календаре" in kb_cal.inline_keyboard[0][1].text

    # Без кнопки календаря
    kb_only_done = bot_module.get_task_inline_keyboard(42, show_calendar_button=False, is_in_calendar=False)
    assert len(kb_only_done.inline_keyboard[0]) == 1
    assert kb_only_done.inline_keyboard[0][0].callback_data == "done:42"


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
    """Проверяет, что команда /tasks возвращает кнопки: с календарем для задач с дедлайном, без календаря для задач без дедлайна."""
    # Задача 1 без дедлайна
    t1 = await bot_module.create_task(title="Задача без дедлайна", db_path=initialized_db)
    # Задача 2 с явным дедлайном
    t2 = await bot_module.create_task(title="Задача с дедлайном", deadline="2026-11-25 15:00:00", db_path=initialized_db)

    fake_message = AsyncMock()
    fake_message.answer = AsyncMock()

    with patch.object(bot_module, "DB_PATH", initialized_db):
        await bot_module.cmd_tasks(fake_message)

    fake_message.answer.assert_called_once()
    reply_markup = fake_message.answer.call_args.kwargs.get("reply_markup")
    assert reply_markup is not None
    assert len(reply_markup.inline_keyboard) == 2

    # Задача 2 (новая, первая в списке) имеет дедлайн -> 2 кнопки (Завершить + В календарь)
    assert reply_markup.inline_keyboard[0][0].callback_data == f"done:{t2}"
    assert reply_markup.inline_keyboard[0][1].callback_data == f"to_calendar:{t2}"

    # Задача 1 (без дедлайна) -> ТОЛЬКО 1 кнопка (Завершить), без кнопки "В календарь"
    assert len(reply_markup.inline_keyboard[1]) == 1
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


# ==============================================================================
# 6. ТЕСТЫ КНОПКИ «В КАЛЕНДАРЬ» И ОПРЕДЕЛЕНИЯ ДАТЫ СОБЫТИЯ
# ==============================================================================


def test_resolve_event_datetime_from_deadline():
    """Проверяет определение даты события по имеющемуся deadline в БД."""
    task = {
        "title": "Отвезти Лену в 19:30 и взять Данию с собой",
        "deadline": "2026-11-21 19:30:00",
        "description": None,
    }
    dt = bot_module.resolve_event_datetime(task)
    assert dt.year == 2026
    assert dt.month == 11
    assert dt.day == 21
    assert dt.hour == 19
    assert dt.minute == 30
    assert dt.tzinfo is not None


def test_resolve_event_datetime_from_title_time():
    """Проверяет извлечение времени из названия задачи при отсутствии дедлайна."""
    task = {
        "title": "Отвезти Лену в 19:30 и взять Данию с собой",
        "deadline": None,
        "description": None,
    }
    dt = bot_module.resolve_event_datetime(task)
    assert dt.hour == 19
    assert dt.minute == 30
    assert dt.tzinfo is not None


def test_resolve_event_datetime_from_title_date_and_time():
    """Проверяет извлечение даты и времени (например: 'Идем к врачу 25 ноября в 9:00')."""
    task = {
        "title": "Идем к врачу 25 ноября в 9:00",
        "deadline": None,
        "description": "Адрес клиники: ул. Пушкина 10",
    }
    dt = bot_module.resolve_event_datetime(task)
    assert dt.day == 25
    assert dt.month == 11
    assert dt.hour == 9
    assert dt.minute == 0


def test_resolve_event_datetime_fallback():
    """Проверяет резервный расчет времени (ближайший круглый час) при отсутствии временных меток."""
    task = {
        "title": "Купить продукты к ужину",
        "deadline": None,
        "description": None,
    }
    dt = bot_module.resolve_event_datetime(task)
    assert dt.minute == 0
    assert dt.second == 0
    assert dt.tzinfo is not None


@pytest.mark.asyncio
async def test_callback_to_calendar_adds_event_and_updates_db(initialized_db):
    """
    Проверяет сценарий ложного срабатывания ИИ: задача создана как 'task',
    пользователь нажимает «📅 В календарь», задача переводится в 'calendar',
    создается событие в Apple Calendar (CalDAV), и обновляются кнопки.
    """
    task_id = await bot_module.create_task(
        title="Отвезти Лену в 19:30 и взять Данию с собой",
        deadline="2026-11-21 19:30:00",
        category="task",
        db_path=initialized_db,
    )

    mock_cal_service = AsyncMock(spec=CalendarSyncService)
    mock_cal_service.is_enabled = True
    mock_cal_service.create_event.return_value = {
        "uid": "cal_event_uid_133",
        "url": "https://caldav.apple.com/events/133.ics",
    }

    callback = AsyncMock()
    callback.data = f"to_calendar:{task_id}"
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "calendar_service", mock_cal_service):

        await bot_module.callback_to_calendar(callback)

    # 1. Проверяем вызов сервиса календаря
    mock_cal_service.create_event.assert_called_once()
    call_kwargs = mock_cal_service.create_event.call_args.kwargs
    assert call_kwargs["title"] == "Отвезти Лену в 19:30 и взять Данию с собой"
    assert call_kwargs["start_dt"].hour == 19
    assert call_kwargs["start_dt"].minute == 30

    # 2. Проверяем сохранение категории и CalDAV данных в SQLite
    task = await bot_module.get_task_by_id(task_id, db_path=initialized_db)
    assert task["category"] == "calendar"
    assert task["caldav_uid"] == "cal_event_uid_133"
    assert task["caldav_url"] == "https://caldav.apple.com/events/133.ics"

    # 3. Проверяем уведомление и замену кнопок под сообщением
    callback.answer.assert_called_once()
    assert "Добавлено в Apple Calendar" in callback.answer.call_args.args[0]
    callback.message.edit_reply_markup.assert_called_once()

    updated_markup = callback.message.edit_reply_markup.call_args.kwargs.get("reply_markup")
    assert updated_markup is not None
    # Первая кнопка - Завершить, вторая - [🗓 В календаре] (заглушка noop_cal)
    assert updated_markup.inline_keyboard[0][0].callback_data == f"done:{task_id}"
    assert updated_markup.inline_keyboard[0][1].callback_data == "noop_cal"
    assert "В календаре" in updated_markup.inline_keyboard[0][1].text


@pytest.mark.asyncio
async def test_callback_to_calendar_already_in_calendar(initialized_db):
    """Проверяет, что повторное нажатие не создает дубликатов в CalDAV."""
    task_id = await bot_module.create_task(
        title="Событие уже в календаре",
        category="calendar",
        caldav_uid="existing_uid_999",
        db_path=initialized_db,
    )

    mock_cal_service = AsyncMock(spec=CalendarSyncService)
    mock_cal_service.is_enabled = True

    callback = AsyncMock()
    callback.data = f"to_calendar:{task_id}"
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "calendar_service", mock_cal_service):

        await bot_module.callback_to_calendar(callback)

    # Сервис не должен вызываться повторно
    mock_cal_service.create_event.assert_not_called()
    callback.answer.assert_called_once()
    assert "уже занесено" in callback.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_callback_noop_cal():
    """Проверяет всплывающий ответ для кнопки '🗓 В календаре'."""
    callback = AsyncMock()
    callback.answer = AsyncMock()

    await bot_module.callback_noop_cal(callback)
    callback.answer.assert_called_once()
    assert "уже занесено" in callback.answer.call_args.args[0]


# ==============================================================================
# 7. ТЕСТЫ ПАГИНАЦИИ /tasks И ТОЧЕЧНОГО ОБНОВЛЕНИЯ КНОПОК
# ==============================================================================


def test_render_tasks_page_empty():
    """Проверяет рендеринг при отсутствии активных задач."""
    text, kb = bot_module.render_tasks_page([])
    assert "Активных задач нет" in text
    assert kb is None


def test_render_tasks_page_pagination_and_size_limit():
    """Проверяет корректное разделение на страницы и ограничение размера сообщения."""
    fake_tasks = [
        {
            "id": i,
            "title": f"Задача номер {i} с длинным названием",
            "status": "pending",
            "deadline": "2026-11-25 10:00:00",
            "category": "task",
            "description": "Очень длинное описание с множеством деталей и ссылок " * 20,
            "location": "Офис на Тверской",
            "contact_info": "+7 999 111-22-33",
            "meeting_url": "https://meet.google.com/abc-def-ghi",
            "trello_card_url": f"https://trello.com/c/card_{i}",
            "caldav_uid": None,
        }
        for i in range(1, 26)
    ]

    # Страница 1 (из 3)
    text_p1, kb_p1 = bot_module.render_tasks_page(fake_tasks, page=0, page_size=10)
    assert "Стр. 1/3" in text_p1
    assert "всего: 25" in text_p1
    assert len(text_p1) < 4000  # Защита от лимита 4096 символов Telegram
    # 10 строк задач + 1 строка навигации
    assert len(kb_p1.inline_keyboard) == 11
    nav_row_p1 = kb_p1.inline_keyboard[-1]
    assert any("Вперед" in btn.text for btn in nav_row_p1)
    assert not any("Назад" in btn.text for btn in nav_row_p1)

    # Страница 2 (из 3)
    text_p2, kb_p2 = bot_module.render_tasks_page(fake_tasks, page=1, page_size=10)
    assert "Стр. 2/3" in text_p2
    assert len(kb_p2.inline_keyboard) == 11
    nav_row_p2 = kb_p2.inline_keyboard[-1]
    assert any("Назад" in btn.text for btn in nav_row_p2)
    assert any("Вперед" in btn.text for btn in nav_row_p2)

    # Страница 3 (из 3, последние 5 задач)
    text_p3, kb_p3 = bot_module.render_tasks_page(fake_tasks, page=2, page_size=10)
    assert "Стр. 3/3" in text_p3
    # 5 строк задач + 1 строка навигации
    assert len(kb_p3.inline_keyboard) == 6
    nav_row_p3 = kb_p3.inline_keyboard[-1]
    assert any("Назад" in btn.text for btn in nav_row_p3)
    assert not any("Вперед" in btn.text for btn in nav_row_p3)


@pytest.mark.asyncio
async def test_callback_tasks_page_updates_message(initialized_db):
    """Проверяет листание страниц через callback tasks_page."""
    for i in range(15):
        await bot_module.create_task(title=f"Тестовая задача {i}", db_path=initialized_db)

    callback = AsyncMock()
    callback.data = "tasks_page:1"
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    await bot_module.callback_tasks_page(callback, db_path=initialized_db)

    callback.message.edit_text.assert_called_once()
    edited_text = callback.message.edit_text.call_args.args[0]
    assert "Стр. 2/2" in edited_text
    callback.answer.assert_called_once()


@pytest.mark.asyncio
async def test_callback_done_task_in_multi_row_list_preserves_other_buttons(initialized_db):
    """
    Проверяет, что при закрытии задачи из списка /tasks обновляется ТОЛЬКО строка
    закрытой задачи, а остальные задачи и кнопки навигации остаются активными!
    """
    t1 = await bot_module.create_task(title="Задача 1", db_path=initialized_db)
    t2 = await bot_module.create_task(title="Задача 2", db_path=initialized_db)

    fake_markup = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text=f"✅ #{t1} Задача 1", callback_data=f"done:{t1}"),
                types.InlineKeyboardButton(text="📅 В календарь", callback_data=f"to_calendar:{t1}"),
            ],
            [
                types.InlineKeyboardButton(text=f"✅ #{t2} Задача 2", callback_data=f"done:{t2}"),
                types.InlineKeyboardButton(text="📅 В календарь", callback_data=f"to_calendar:{t2}"),
            ],
        ]
    )

    callback = AsyncMock()
    callback.data = f"done:{t1}"
    callback.message = AsyncMock()
    callback.message.reply_markup = fake_markup
    callback.answer = AsyncMock()

    with patch.object(bot_module, "DB_PATH", initialized_db):
        await bot_module.callback_done_task(callback)

    callback.message.edit_reply_markup.assert_called_once()
    new_markup = callback.message.edit_reply_markup.call_args.kwargs.get("reply_markup")
    assert len(new_markup.inline_keyboard) == 2

    # Строка 1: обновлена на статус Завершена
    row1 = new_markup.inline_keyboard[0]
    assert len(row1) == 1
    assert f"#{t1} Завершена" in row1[0].text
    assert row1[0].callback_data == "noop"

    # Строка 2: осталась неизменной и активной!
    row2 = new_markup.inline_keyboard[1]
    assert len(row2) == 2
    assert row2[0].callback_data == f"done:{t2}"
    assert row2[1].callback_data == f"to_calendar:{t2}"


@pytest.mark.asyncio
async def test_callback_to_calendar_in_multi_row_list_preserves_other_buttons(initialized_db):
    """
    Проверяет, что при переносе в календарь из списка /tasks обновляется ТОЛЬКО строка
    перенесенной задачи на [🗓 В календаре], а остальные кнопки остаются активными!
    """
    t1 = await bot_module.create_task(title="Встреча 1 в 14:00", deadline="2026-11-21 14:00:00", db_path=initialized_db)
    t2 = await bot_module.create_task(title="Задача 2", db_path=initialized_db)

    fake_markup = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text=f"✅ #{t1} Встреча 1", callback_data=f"done:{t1}"),
                types.InlineKeyboardButton(text="📅 В календарь", callback_data=f"to_calendar:{t1}"),
            ],
            [
                types.InlineKeyboardButton(text=f"✅ #{t2} Задача 2", callback_data=f"done:{t2}"),
                types.InlineKeyboardButton(text="📅 В календарь", callback_data=f"to_calendar:{t2}"),
            ],
        ]
    )

    mock_cal_service = AsyncMock(spec=CalendarSyncService)
    mock_cal_service.is_enabled = True
    mock_cal_service.create_event.return_value = {"uid": "cal_111", "url": "https://..."}

    callback = AsyncMock()
    callback.data = f"to_calendar:{t1}"
    callback.message = AsyncMock()
    callback.message.reply_markup = fake_markup
    callback.answer = AsyncMock()

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "calendar_service", mock_cal_service):
        await bot_module.callback_to_calendar(callback)

    callback.message.edit_reply_markup.assert_called_once()
    new_markup = callback.message.edit_reply_markup.call_args.kwargs.get("reply_markup")
    assert len(new_markup.inline_keyboard) == 2

    # Строка 1: кнопка календаря стала [🗓 В календаре]
    row1 = new_markup.inline_keyboard[0]
    assert len(row1) == 2
    assert row1[0].callback_data == f"done:{t1}"
    assert row1[1].callback_data == "noop_cal"
    assert "В календаре" in row1[1].text

    # Строка 2: осталась неизменной!
    row2 = new_markup.inline_keyboard[1]
    assert len(row2) == 2
    assert row2[0].callback_data == f"done:{t2}"
    assert row2[1].callback_data == f"to_calendar:{t2}"


# ==============================================================================
# 8. ТЕСТЫ ПРОВЕРКИ CalDAV ПРИ ЗАПУСКЕ (FALL FAST И УСПЕШНОЕ ПОДКЛЮЧЕНИЕ)
# ==============================================================================


@pytest.mark.asyncio
async def test_caldav_check_connection_success():
    """Проверяет успешное прохождение проверки связи с CalDAV и выбор календаря."""
    mock_cal = MagicMock()
    mock_cal.name = "Personal"

    mock_principal = MagicMock()
    mock_principal.calendars.return_value = [mock_cal]

    mock_client = MagicMock()
    mock_client.principal.return_value = mock_principal

    service = CalendarSyncService(
        url="https://caldav.icloud.com/",
        username="user@icloud.com",
        password="app-specific-password",
        calendar_name="Personal",
        enabled=True,
    )

    with patch("caldav.DAVClient", return_value=mock_client):
        active_cal = await service.check_connection()

    assert active_cal == "Personal"
    assert service.active_calendar_name == "Personal"


@pytest.mark.asyncio
async def test_caldav_check_connection_disabled():
    """Проверяет, что при enabled=False проверка не выполняется."""
    service = CalendarSyncService(enabled=False)
    res = await service.check_connection()
    assert res == ""


@pytest.mark.asyncio
async def test_caldav_check_connection_missing_credentials_raises():
    """Проверяет, что при включенном CalDAV без учетных данных выбрасывается ошибка."""
    service = CalendarSyncService(
        url="",
        username="",
        password="",
        enabled=True,
    )
    with pytest.raises(RuntimeError) as exc_info:
        await service.check_connection()
    assert "не заданы обязательные параметры" in str(exc_info.value)


@pytest.mark.asyncio
async def test_caldav_check_connection_network_or_auth_error_raises():
    """Проверяет, что при ошибке сети/авторизации выбрасывается RuntimeError."""
    service = CalendarSyncService(
        url="https://caldav.icloud.com/",
        username="user@icloud.com",
        password="bad-password",
        enabled=True,
    )
    with patch("caldav.DAVClient", side_effect=Exception("401 Unauthorized")):
        with pytest.raises(RuntimeError) as exc_info:
            await service.check_connection()
    assert "Не удалось подключиться к CalDAV серверу" in str(exc_info.value)


@pytest.mark.asyncio
async def test_caldav_check_connection_calendar_not_found_raises():
    """Проверяет, что если указанного в конфиге календаря нет на сервере — бот падает с ошибкой."""
    mock_cal = MagicMock()
    mock_cal.name = "Work"

    mock_principal = MagicMock()
    mock_principal.calendars.return_value = [mock_cal]

    mock_client = MagicMock()
    mock_client.principal.return_value = mock_principal

    service = CalendarSyncService(
        url="https://caldav.icloud.com/",
        username="user@icloud.com",
        password="password",
        calendar_name="Personal",
        enabled=True,
    )

    with patch("caldav.DAVClient", return_value=mock_client):
        with pytest.raises(RuntimeError) as exc_info:
            await service.check_connection()
    assert "не найден на сервере CalDAV" in str(exc_info.value)


@pytest.mark.asyncio
async def test_on_startup_fails_fast_when_caldav_broken(initialized_db):
    """
    Проверяет, что при старте бота, если CalDAV включен в конфиге, но связи нет —
    on_startup аварийно падает (fail-fast), предотвращая работу с битым календарем.
    """
    mock_bot = AsyncMock()
    mock_cal_service = AsyncMock(spec=CalendarSyncService)
    mock_cal_service.enabled = True
    mock_cal_service.check_connection.side_effect = RuntimeError("CalDAV server unreachable")
    mock_scheduler = MagicMock()
    mock_scheduler.running = True

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "scheduler", mock_scheduler), \
         patch.object(bot_module, "calendar_service", mock_cal_service), \
         patch.object(bot_module, "trello_service", None):

        with pytest.raises(RuntimeError) as exc_info:
            await bot_module.on_startup(mock_bot)

        assert "CalDAV server unreachable" in str(exc_info.value)
        mock_cal_service.check_connection.assert_called_once()


@pytest.mark.asyncio
async def test_on_startup_succeeds_when_caldav_ok(initialized_db):
    """Проверяет, что при успешной проверке CalDAV бот стартует штатно."""
    mock_bot = AsyncMock()
    mock_cal_service = AsyncMock(spec=CalendarSyncService)
    mock_cal_service.enabled = True
    mock_cal_service.check_connection.return_value = "Family"
    mock_scheduler = MagicMock()
    mock_scheduler.running = True

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "scheduler", mock_scheduler), \
         patch.object(bot_module, "calendar_service", mock_cal_service), \
         patch.object(bot_module, "trello_service", None):

        await bot_module.on_startup(mock_bot)
        mock_cal_service.check_connection.assert_called_once()
