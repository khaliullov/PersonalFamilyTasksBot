import asyncio
import base64
from datetime import datetime, timedelta
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
import pytest

import aiohttp
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import bot as bot_module
from trello.client import TrelloClient
from trello.config import parse_list_names
from trello.matcher import (
    find_all_matching_lists,
    find_matching_list,
    is_list_name_matching,
    normalize_column_name,
)
from trello.service import TrelloSyncService
from trello.webhook import TrelloWebhookServer, verify_trello_webhook_signature
from tests.conftest import make_message


# ==============================================================================
# 1. ТЕСТЫ ПАРСИНГА КОНФИГУРАЦИИ И РАЗДЕЛИТЕЛЕЙ
# ==============================================================================


def test_parse_list_names_defaults():
    raw = "Inbox, Входящие, Новые, To Do"
    parsed = parse_list_names(raw, separator=",")
    assert parsed == ["Inbox", "Входящие", "Новые", "To Do"]


def test_parse_list_names_custom_separator():
    raw = "Done; Завершено ; Выполнено ;Готово"
    parsed = parse_list_names(raw, separator=";")
    assert parsed == ["Done", "Завершено", "Выполнено", "Готово"]


def test_parse_list_names_empty_and_whitespace():
    assert parse_list_names("") == []
    assert parse_list_names(None) == []
    assert parse_list_names("  , , , ") == []


# ==============================================================================
# 2. ТЕСТЫ СОПОСТАВЛЕНИЯ КОЛОНОК (КИРИЛЛИЦА, ЛАТИНИЦА, ЭМОДЗИ, РЕГИСТР)
# ==============================================================================


def test_normalize_column_name():
    assert normalize_column_name("📥 Входящие задачи") == "входящие задачи"
    assert normalize_column_name("Done / Завершено!") == "done завершено"
    assert normalize_column_name("  TO DO  ") == "to do"
    assert normalize_column_name("✅ Выполнено") == "выполнено"


def test_is_list_name_matching():
    candidates = ["Inbox", "Входящие", "To Do"]

    # Точные совпадения без учета регистра
    assert is_list_name_matching("Inbox", candidates) is True
    assert is_list_name_matching("inbox", candidates) is True
    assert is_list_name_matching("ВХОДЯЩИЕ", candidates) is True
    assert is_list_name_matching("входящие", candidates) is True

    # Совпадения с эмодзи и дополнительными словами
    assert is_list_name_matching("📥 Входящие", candidates) is True
    assert is_list_name_matching("Входящие задачи", candidates) is True
    assert is_list_name_matching("📋 To Do list", candidates) is True

    # Несовпадающие колонки
    assert is_list_name_matching("В работе", candidates) is False
    assert is_list_name_matching("Done", candidates) is False
    assert is_list_name_matching("Завершено", candidates) is False


def test_find_matching_list():
    board_lists = [
        {"id": "list_1", "name": "📥 Входящие", "closed": False},
        {"id": "list_2", "name": "В работе", "closed": False},
        {"id": "list_3", "name": "✅ Выполнено", "closed": False},
        {"id": "list_4", "name": "Inbox (старый)", "closed": True},  # closed - должен быть пропущен
    ]

    inbox = find_matching_list(board_lists, ["Inbox", "Входящие"])
    assert inbox is not None
    assert inbox["id"] == "list_1"

    # Проверка, что закрытый список не выбирается
    old = find_matching_list(board_lists, ["старый"])
    assert old is None


def test_find_all_matching_lists():
    board_lists = [
        {"id": "list_1", "name": "Inbox", "closed": False},
        {"id": "list_2", "name": "Done", "closed": False},
        {"id": "list_3", "name": "Выполнено", "closed": False},
        {"id": "list_4", "name": "Архив выполненных", "closed": False},
        {"id": "list_5", "name": "В работе", "closed": False},
    ]

    done_lists = find_all_matching_lists(board_lists, ["Done", "Выполнено"])
    assert len(done_lists) == 3
    ids = {d["id"] for d in done_lists}
    assert ids == {"list_2", "list_3", "list_4"}


# ==============================================================================
# 3. ТЕСТЫ КРИПТОГРАФИЧЕСКОЙ ПОДПИСИ WEBHOOK (HMAC-SHA1)
# ==============================================================================


def test_verify_trello_webhook_signature():
    secret = "my_trello_app_secret_12345"
    callback_url = "https://example.com/trello/webhook"
    request_body = json.dumps({"action": {"type": "updateCard"}})

    # Вычисляем правильную подпись
    content = (request_body + callback_url).encode("utf-8")
    computed_hmac = hmac.new(secret.encode("utf-8"), content, hashlib.sha1)
    valid_signature = base64.b64encode(computed_hmac.digest()).decode("utf-8")

    # 1. Валидная подпись
    assert verify_trello_webhook_signature(
        request_body=request_body,
        callback_url=callback_url,
        secret=secret,
        header_signature=valid_signature,
    ) is True

    # 2. Невалидная подпись
    assert verify_trello_webhook_signature(
        request_body=request_body,
        callback_url=callback_url,
        secret=secret,
        header_signature="invalid_signature_base64==",
    ) is False

    # 3. Подпись от другого тела
    assert verify_trello_webhook_signature(
        request_body=request_body + "tampered",
        callback_url=callback_url,
        secret=secret,
        header_signature=valid_signature,
    ) is False

    # 4. Пустой секрет или подпись
    assert verify_trello_webhook_signature(request_body, callback_url, "", valid_signature) is False
    assert verify_trello_webhook_signature(request_body, callback_url, secret, "") is False


# ==============================================================================
# 4. ТЕСТЫ TRELLO REST CLIENT
# ==============================================================================


@pytest.mark.asyncio
async def test_trello_client_methods():
    client = TrelloClient(api_key="test_key", token="test_token")

    def make_mock_session(json_data):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=json_data)
        mock_resp.text = AsyncMock(return_value="")

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_cm)
        mock_session.post = MagicMock(return_value=mock_cm)
        mock_session.put = MagicMock(return_value=mock_cm)
        return mock_session

    # 1. get_board_lists
    fake_lists = [
        {"id": "list_inbox", "name": "Входящие", "closed": False},
        {"id": "list_done", "name": "Выполнено", "closed": False},
    ]
    with patch.object(client, "_get_session", new=AsyncMock(return_value=make_mock_session(fake_lists))):
        lists = await client.get_board_lists("board_123")
        assert len(lists) == 2
        assert lists[0]["name"] == "Входящие"

    # 2. create_card
    fake_created = {
        "id": "card_999",
        "name": "Купить продукты",
        "shortUrl": "https://trello.com/c/abc12345",
    }
    with patch.object(client, "_get_session", new=AsyncMock(return_value=make_mock_session(fake_created))):
        card = await client.create_card(
            list_id="list_inbox",
            title="Купить продукты",
            description="Молоко, сыр",
            due=datetime(2026, 12, 1, 18, 0, 0),
        )
        assert card is not None
        assert card["id"] == "card_999"
        assert card["shortUrl"] == "https://trello.com/c/abc12345"

    # 3. update_card
    fake_updated = {"id": "card_999", "name": "Купить продукты (обновлено)"}
    with patch.object(client, "_get_session", new=AsyncMock(return_value=make_mock_session(fake_updated))):
        updated = await client.update_card(
            card_id="card_999",
            title="Купить продукты (обновлено)",
        )
        assert updated is not None
        assert updated["name"] == "Купить продукты (обновлено)"

    await client.close()


# ==============================================================================
# 5. ТЕСТЫ ВСТРОЕННОГО WEBHOOK HTTP СЕРВЕРА
# ==============================================================================


@pytest.mark.asyncio
async def test_webhook_server_http_lifecycle():
    handled_events = []

    async def mock_handler(event):
        handled_events.append(event)

    # Используем порт 0 (ОС автоматически выделит свободный порт)
    server = TrelloWebhookServer(
        event_handler=mock_handler,
        host="127.0.0.1",
        port=0,
        path="/trello/webhook",
    )

    started = await server.start()
    assert started is True
    # Получаем фактический порт
    port = server._site._server.sockets[0].getsockname()[1]

    try:
        async with aiohttp.ClientSession() as http:
            base_url = f"http://127.0.0.1:{port}/trello/webhook"

            # 1. Trello HEAD проверка
            async with http.head(base_url) as resp:
                assert resp.status == 200

            # 2. GET проверка
            async with http.get(base_url) as resp:
                assert resp.status == 200
                text = await resp.text()
                assert "Trello Webhook Server is running" in text

            # 3. POST полезная нагрузка
            payload = {
                "action": {
                    "type": "updateCard",
                    "data": {
                        "card": {"id": "card_test_1", "name": "Тестовая задача"},
                        "listAfter": {"id": "done_list_id", "name": "Done"},
                    },
                }
            }
            async with http.post(base_url, json=payload) as resp:
                assert resp.status == 200

            # Даем время на выполнение dispatch в таске
            await asyncio.sleep(0.05)
            assert len(handled_events) == 1
            assert handled_events[0]["action"]["type"] == "updateCard"
    except (aiohttp.ClientConnectorError, PermissionError):
        from aiohttp.test_utils import make_mocked_request
        payload = {
            "action": {
                "type": "updateCard",
                "data": {
                    "card": {"id": "card_test_1", "name": "Тестовая задача"},
                    "listAfter": {"id": "done_list_id", "name": "Done"},
                },
            }
        }
        head_req = make_mocked_request("HEAD", "/trello/webhook", app=server._app)
        head_resp = await server._handle_head(head_req)
        assert head_resp.status == 200

        get_req = make_mocked_request("GET", "/trello/webhook", app=server._app)
        get_resp = await server._handle_get(get_req)
        assert get_resp.status == 200
        assert "Trello Webhook Server is running" in get_resp.text

        post_req = make_mocked_request("POST", "/trello/webhook", app=server._app)
        post_req.text = AsyncMock(return_value=json.dumps(payload))
        post_req.read = AsyncMock(return_value=json.dumps(payload).encode("utf-8"))
        post_resp = await server._handle_post(post_req)
        assert post_resp.status == 200

        await asyncio.sleep(0.05)
        assert len(handled_events) == 1
        assert handled_events[0]["action"]["type"] == "updateCard"

    await server.stop()
    assert server._is_running is False


# ==============================================================================
# 6. ТЕСТЫ TRELLO SYNC SERVICE (ЛОГИКА ОБРАБОТКИ СОБЫТИЙ И СКАНИРОВАНИЯ)
# ==============================================================================


@pytest.mark.asyncio
async def test_trello_service_resolve_lists():
    mock_client = AsyncMock(spec=TrelloClient)
    mock_client.get_board_lists.return_value = [
        {"id": "id_inbox", "name": "📥 Входящие", "closed": False},
        {"id": "id_wip", "name": "В работе", "closed": False},
        {"id": "id_done_1", "name": "Done", "closed": False},
        {"id": "id_done_2", "name": "✅ Выполнено", "closed": False},
    ]

    service = TrelloSyncService(
        api_key="key",
        token="token",
        board_id="board_123",
        inbox_candidates=["Inbox", "Входящие"],
        done_candidates=["Done", "Выполнено"],
        client=mock_client,
    )

    resolved = await service.resolve_board_lists()
    assert resolved is True
    assert service.inbox_list_id == "id_inbox"
    assert service.inbox_list_name == "📥 Входящие"
    assert service.done_list_ids == {"id_done_1", "id_done_2"}


@pytest.mark.asyncio
async def test_trello_service_webhook_card_completed():
    completed_cards = []

    async def on_card_completed(card_id: str, card_name: str):
        completed_cards.append((card_id, card_name))

    service = TrelloSyncService(
        api_key="key",
        token="token",
        board_id="board_123",
        inbox_candidates=["Inbox", "Входящие"],
        done_candidates=["Done", "Выполнено"],
    )
    service.done_list_ids = {"done_list_99"}
    service._on_card_completed_callback = on_card_completed

    # Сценарий 1: Карточка перемещена в Done (updateCard)
    payload_moved_to_done = {
        "action": {
            "type": "updateCard",
            "data": {
                "card": {"id": "card_abc", "name": "Сделать отчет"},
                "listBefore": {"id": "inbox_list", "name": "Inbox"},
                "listAfter": {"id": "done_list_99", "name": "Done"},
            },
        }
    }
    await service.handle_webhook_payload(payload_moved_to_done)
    assert len(completed_cards) == 1
    assert completed_cards[0] == ("card_abc", "Сделать отчет")

    # Сценарий 2: Карточка перемещена в колонку не-Done ("В работе") -> НЕ должна вызывать completion
    payload_moved_to_wip = {
        "action": {
            "type": "updateCard",
            "data": {
                "card": {"id": "card_xyz", "name": "Вторая задача"},
                "listBefore": {"id": "inbox_list", "name": "Inbox"},
                "listAfter": {"id": "wip_list", "name": "В работе"},
            },
        }
    }
    await service.handle_webhook_payload(payload_moved_to_wip)
    assert len(completed_cards) == 1  # количество не увеличилось

    # Сценарий 3: Карточка создана сразу в Done (createCard)
    payload_created_in_done = {
        "action": {
            "type": "createCard",
            "data": {
                "card": {"id": "card_created_in_done", "name": "Задача закрытая сразу"},
                "list": {"id": "done_list_99", "name": "Done"},
            },
        }
    }
    await service.handle_webhook_payload(payload_created_in_done)
    assert len(completed_cards) == 2
    assert completed_cards[1] == ("card_created_in_done", "Задача закрытая сразу")


@pytest.mark.asyncio
async def test_trello_service_polling_fallback():
    completed_cards = []

    async def on_card_completed(card_id: str, card_name: str):
        completed_cards.append((card_id, card_name))

    mock_client = AsyncMock(spec=TrelloClient)
    # Возвращаем 2 карточки в колонке Done
    mock_client.get_cards_in_list.return_value = [
        {"id": "card_poll_1", "name": "Купить молоко"},
        {"id": "card_poll_2", "name": "Оплатить интернет"},
    ]

    service = TrelloSyncService(
        api_key="key",
        token="token",
        board_id="board_123",
        client=mock_client,
    )
    service.done_list_ids = {"done_list_poll"}
    service._on_card_completed_callback = on_card_completed

    # Запускаем опрос
    with patch.object(TrelloSyncService, "is_enabled", new_callable=PropertyMock, return_value=True):
        await service.poll_done_columns()

    assert len(completed_cards) == 2
    assert completed_cards[0] == ("card_poll_1", "Купить молоко")
    assert completed_cards[1] == ("card_poll_2", "Оплатить интернет")


# ==============================================================================
# 7. ИНТЕГРАЦИОННЫЕ ТЕСТЫ БОТА С TRELLO И БАЗОЙ ДАННЫХ
# ==============================================================================


@pytest.mark.asyncio
async def test_bot_database_trello_fields_and_helpers(initialized_db):
    """
    Проверяет сохранение trello_card_id, trello_card_url и поиск задачи по ним.
    """
    task_id = await bot_module.create_task(
        title="Купить цветы",
        description="Красные розы",
        db_path=initialized_db,
    )

    # 1. Привязка к Trello
    card_id = "trello_card_flower_123"
    card_url = "https://trello.com/c/flowers"
    ok = await bot_module.update_task_trello_info(
        task_id=task_id,
        trello_card_id=card_id,
        trello_card_url=card_url,
        db_path=initialized_db,
    )
    assert ok is True

    # 2. Поиск по trello_card_id
    task = await bot_module.get_task_by_trello_card_id(card_id, db_path=initialized_db)
    assert task is not None
    assert task["id"] == task_id
    assert task["trello_card_id"] == card_id
    assert task["trello_card_url"] == card_url

    # 3. Поиск через find_active_task_for_trello_card
    # Вариант а: по прямому card_id
    found_by_id = await bot_module.find_active_task_for_trello_card(
        card_id=card_id,
        card_title="Не имеет значения",
        db_path=initialized_db,
    )
    assert found_by_id is not None
    assert found_by_id["id"] == task_id

    # Вариант б: по номеру #ID в заголовке карточки
    found_by_regex = await bot_module.find_active_task_for_trello_card(
        card_id="unknown_card",
        card_title=f"Купить цветы [#{task_id}]",
        db_path=initialized_db,
    )
    assert found_by_regex is not None
    assert found_by_regex["id"] == task_id

    # Вариант в: по точному совпадению названия задачи
    found_by_title = await bot_module.find_active_task_for_trello_card(
        card_id="unknown_card",
        card_title="Купить цветы",
        db_path=initialized_db,
    )
    assert found_by_title is not None
    assert found_by_title["id"] == task_id


@pytest.mark.asyncio
async def test_bot_handle_trello_card_completed_closes_task(initialized_db, mock_bot):
    """
    Проверяет, что при вызове handle_trello_card_completed:
    1. Статус задачи в SQLite становится 'completed'.
    2. Таймер в APScheduler снимается.
    3. Мужу отправляется уведомление в Telegram с подтверждением.
    """
    task_id = await bot_module.create_task(
        title="Оплатить налоги",
        db_path=initialized_db,
    )
    card_id = "trello_card_taxes_555"
    await bot_module.update_task_trello_info(
        task_id=task_id,
        trello_card_id=card_id,
        trello_card_url="https://trello.com/c/taxes",
        db_path=initialized_db,
    )

    # Регистрируем фиктивную задачу в планировщике
    scheduler = bot_module.scheduler
    if not scheduler.running:
        scheduler.start(paused=True)

    job_id = f"task_reminder_{task_id}"
    scheduler.add_job(
        lambda: None,
        trigger="date",
        run_date=datetime.now() + timedelta(hours=5),
        id=job_id,
        replace_existing=True,
    )
    assert scheduler.get_job(job_id) is not None

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "_current_bot", mock_bot):

        await bot_module.handle_trello_card_completed(card_id, "Оплатить налоги")

    # 1. Проверяем статус в SQLite
    updated_task = await bot_module.get_task_by_id(task_id, db_path=initialized_db)
    assert updated_task["status"] == "completed"

    # 2. Проверяем, что задача снята из планировщика
    assert scheduler.get_job(job_id) is None

    # 3. Проверяем отправку сообщения мужу
    mock_bot.send_message.assert_called_once()
    sent_text = mock_bot.send_message.call_args.kwargs["text"]
    assert f"Задача #{task_id} выполнена в Trello" in sent_text
    assert "Оплатить налоги" in sent_text


@pytest.mark.asyncio
async def test_bot_creates_task_and_syncs_to_trello(initialized_db, mock_bot):
    """
    Проверяет полный цикл:
    1. Жена пишет сообщение с задачей.
    2. Бот создает задачу в SQLite.
    3. Создается карточка в Trello в колонке Inbox.
    4. Уведомление мужу содержит ссылку на карточку Trello.
    """
    wife_msg = make_message(
        message_id=501,
        user_id=bot_module.WIFE_ID,
        text="Забери пожалуйста посылку на почте до 19:00",
    )

    ollama_response = {
        "action": "create",
        "is_task": True,
        "task_id": None,
        "task_title": "Забрать посылку на почте",
        "task_description": "Паспорт с собой",
        "has_deadline": False,
        "deadline_timestamp": None,
    }

    mock_trello_service = AsyncMock(spec=TrelloSyncService)
    mock_trello_service.is_enabled = True
    mock_trello_service.sync_task_created.return_value = {
        "card_id": "card_parcel_777",
        "card_url": "https://trello.com/c/parcel777",
    }

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "DEBOUNCE_DELAY_SECONDS", 0.01), \
         patch.object(bot_module, "trello_service", mock_trello_service), \
         patch("bot.analyze_context_with_ollama", new=AsyncMock(return_value=ollama_response)):

        await bot_module.handle_wife_business_message(wife_msg, mock_bot)
        await asyncio.sleep(0.05)

    # 1. Проверяем создание в SQLite
    tasks = await bot_module.get_active_tasks(db_path=initialized_db)
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Забрать посылку на почте"
    assert tasks[0]["trello_card_id"] == "card_parcel_777"
    assert tasks[0]["trello_card_url"] == "https://trello.com/c/parcel777"

    # 2. Проверяем вызов sync_task_created
    mock_trello_service.sync_task_created.assert_called_once()
    assert mock_trello_service.sync_task_created.call_args.kwargs["title"] == "Забрать посылку на почте"

    # 3. Проверяем ссылку на Trello в сообщении мужу
    mock_bot.send_message.assert_called_once()
    sent_text = mock_bot.send_message.call_args.kwargs["text"]
    assert "https://trello.com/c/parcel777" in sent_text
    assert "Открыть карточку в Trello" in sent_text


@pytest.mark.asyncio
async def test_cmd_done_moves_card_in_trello(initialized_db):
    """
    Проверяет, что при выполнении команды /done <id>:
    1. Задача помечается как completed в SQLite.
    2. Карточка в Trello перемещается в колонку Done.
    """
    task_id = await bot_module.create_task(
        title="Купить кофе",
        db_path=initialized_db,
    )
    card_id = "card_coffee_101"
    await bot_module.update_task_trello_info(
        task_id=task_id,
        trello_card_id=card_id,
        trello_card_url="https://trello.com/c/coffee",
        db_path=initialized_db,
    )

    fake_message = AsyncMock()
    fake_message.text = f"/done {task_id}"
    fake_message.answer = AsyncMock()

    mock_client = AsyncMock(spec=TrelloClient)
    mock_trello_service = AsyncMock(spec=TrelloSyncService)
    mock_trello_service.is_enabled = True
    mock_trello_service.done_list_ids = {"done_list_id_1"}
    mock_trello_service.client = mock_client

    with patch.object(bot_module, "DB_PATH", initialized_db), \
         patch.object(bot_module, "trello_service", mock_trello_service):

        await bot_module.cmd_done(fake_message)

    # 1. Проверяем статус в базе
    task = await bot_module.get_task_by_id(task_id, db_path=initialized_db)
    assert task["status"] == "completed"

    # 2. Проверяем вызов update_card в Trello
    mock_client.update_card.assert_called_once_with(
        card_id=card_id,
        list_id="done_list_id_1",
    )
    fake_message.answer.assert_called_once()
    assert "отмечена как выполненная" in fake_message.answer.call_args.args[0]
