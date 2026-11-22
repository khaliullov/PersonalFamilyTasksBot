import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiohttp.test_utils import make_mocked_request
from yarl import URL

from database import create_task, get_task_by_id, init_db
from web.api import enrich_task_fields
from web.config import DB_PATH_KEY, normalize_prefix
from web.server import WebServer
from web.ui import render_html_page


async def dispatch_request(app, method: str, path: str, json_data=None, query=None):
    """
    Вспомогательная функция для безопасного тестирования aiohttp запросов без сетевых сокетов.
    """
    url_path = path
    if query:
        query_str = "&".join(f"{k}={v}" for k, v in query.items())
        url_path = f"{path}?{query_str}"

    req = make_mocked_request(
        method,
        url_path,
        headers={"Content-Type": "application/json"} if json_data is not None else {},
        app=app,
    )
    if json_data is not None:
        raw_text = json.dumps(json_data)
        req.text = AsyncMock(return_value=raw_text)
        req.read = AsyncMock(return_value=raw_text.encode("utf-8"))
        req.json = AsyncMock(return_value=json_data)

    match_info = await app.router.resolve(req)
    match_info._current_app = app
    req._match_info = match_info
    return await match_info.handler(req)


def test_prefix_normalization():
    """Проверка нормализации префиксов маршрутизации."""
    assert normalize_prefix("/pftb") == "/pftb"
    assert normalize_prefix("pftb") == "/pftb"
    assert normalize_prefix("/pftb/") == "/pftb"
    assert normalize_prefix("pftb/v2/") == "/pftb/v2"
    assert normalize_prefix("") == ""
    assert normalize_prefix("/") == ""
    assert normalize_prefix(None) == ""


def test_web_ui_html_rendering():
    """Проверка генерации HTML страницы Web UI."""
    html = render_html_page(prefix="/pftb", api_prefix="/pftb/api/v1")
    assert "<!DOCTYPE html>" in html
    assert "Personal Family Tasks Bot" in html
    assert "/pftb" in html
    assert "/pftb/api/v1" in html
    assert "Завершить" in html
    assert "В календарь" in html
    assert "data-theme" in html


def test_enrich_task_fields():
    """Проверка вычисления полей доступности действий для Web UI."""
    # Задача с дедлайном
    t1 = {"id": 1, "status": "pending", "deadline": "2026-11-25 18:00:00", "caldav_uid": None}
    e1 = enrich_task_fields(t1)
    assert e1["has_deadline"] is True
    assert e1["is_in_calendar"] is False
    assert e1["can_complete"] is True
    assert e1["can_add_to_calendar"] is True

    # Задача БЕЗ дедлайна (по аналогии с Telegram - в календарь добавить нельзя)
    t2 = {"id": 2, "status": "pending", "deadline": None, "caldav_uid": None}
    e2 = enrich_task_fields(t2)
    assert e2["has_deadline"] is False
    assert e2["is_in_calendar"] is False
    assert e2["can_complete"] is True
    assert e2["can_add_to_calendar"] is False

    # Задача уже в календаре
    t3 = {"id": 3, "status": "pending", "deadline": "2026-11-25 18:00:00", "caldav_uid": "cal_123"}
    e3 = enrich_task_fields(t3)
    assert e3["is_in_calendar"] is True
    assert e3["can_add_to_calendar"] is False

    # Задача уже завершена
    t4 = {"id": 4, "status": "completed", "deadline": "2026-11-25 18:00:00", "caldav_uid": None}
    e4 = enrich_task_fields(t4)
    assert e4["can_complete"] is False
    assert e4["can_add_to_calendar"] is False


@pytest.mark.asyncio
async def test_web_server_routes_and_redirects():
    """Проверка регистрации маршрутов и редиректов сервера."""
    server = WebServer(prefix="/pftb", port=8080)
    app = server.create_app()

    # 1. Редирект с корня / на /pftb/
    resp_root = await dispatch_request(app, "GET", "/")
    assert resp_root.status == 302
    assert resp_root.headers.get("Location") == "/pftb/"

    # 2. Редирект с /pftb на /pftb/
    resp_prefix = await dispatch_request(app, "GET", "/pftb")
    assert resp_prefix.status == 302
    assert resp_prefix.headers.get("Location") == "/pftb/"

    # 3. Web UI на /pftb/
    resp_ui = await dispatch_request(app, "GET", "/pftb/")
    assert resp_ui.status == 200
    assert "Personal Family Tasks Bot" in resp_ui.text

    # 4. API Health на /pftb/api/v1/health
    resp_health = await dispatch_request(app, "GET", "/pftb/api/v1/health")
    assert resp_health.status == 200
    data = json.loads(resp_health.text)
    assert data["status"] == "ok"
    assert data["prefix"] == "/pftb"
    assert data["api_prefix"] == "/pftb/api/v1"


@pytest.mark.asyncio
async def test_custom_prefix_routing():
    """Проверка работы сервера с произвольным префиксом (например, /bot_alpha)."""
    server = WebServer(prefix="/bot_alpha", port=8080)
    app = server.create_app()

    resp_root = await dispatch_request(app, "GET", "/")
    assert resp_root.status == 302
    assert resp_root.headers.get("Location") == "/bot_alpha/"

    resp_ui = await dispatch_request(app, "GET", "/bot_alpha/")
    assert resp_ui.status == 200

    resp_health = await dispatch_request(app, "GET", "/bot_alpha/api/v1/health")
    assert resp_health.status == 200
    data = json.loads(resp_health.text)
    assert data["prefix"] == "/bot_alpha"


@pytest.mark.asyncio
async def test_api_tasks_pagination_and_filtering(initialized_db):
    """Проверка REST API списка задач, пагинации, фильтрации и поиска."""
    # Создаем тестовые задачи
    await create_task("Купить хлеб", "Белый свежий батон", category="buy", db_path=initialized_db)
    await create_task("Встреча с врачом", "Стоматолог в 15:00", deadline="2026-11-25 15:00:00", category="calendar", db_path=initialized_db)
    await create_task("Оплатить счета", "Коммуналка за ноябрь", category="task", db_path=initialized_db)

    server = WebServer(prefix="/pftb", db_path=initialized_db)
    app = server.create_app()

    # 1. Запрос первой страницы с limit=2
    resp = await dispatch_request(app, "GET", "/pftb/api/v1/tasks", query={"page": "1", "per_page": "2"})
    assert resp.status == 200
    data = json.loads(resp.text)
    assert data["success"] is True
    assert len(data["tasks"]) == 2
    assert data["pagination"]["total"] == 3
    assert data["pagination"]["total_pages"] == 2
    assert data["pagination"]["page"] == 1
    assert data["pagination"]["has_next"] is True
    assert data["pagination"]["has_prev"] is False

    # 2. Запрос второй страницы
    resp2 = await dispatch_request(app, "GET", "/pftb/api/v1/tasks", query={"page": "2", "per_page": "2"})
    data2 = json.loads(resp2.text)
    assert len(data2["tasks"]) == 1
    assert data2["pagination"]["has_next"] is False
    assert data2["pagination"]["has_prev"] is True

    # 3. Фильтрация по категории
    resp_cat = await dispatch_request(app, "GET", "/pftb/api/v1/tasks", query={"category": "buy"})
    data_cat = json.loads(resp_cat.text)
    assert len(data_cat["tasks"]) == 1
    assert data_cat["tasks"][0]["title"] == "Купить хлеб"

    # 4. Поиск
    resp_search = await dispatch_request(app, "GET", "/pftb/api/v1/tasks", query={"search": "стоматолог"})
    data_search = json.loads(resp_search.text)
    assert len(data_search["tasks"]) == 1
    assert data_search["tasks"][0]["title"] == "Встреча с врачом"


@pytest.mark.asyncio
async def test_api_task_detail_and_stats(initialized_db):
    """Проверка получения детальной информации и статистики задач."""
    t_id = await create_task("Тест детали", "Подробное описание", db_path=initialized_db)

    server = WebServer(prefix="/pftb", db_path=initialized_db)
    app = server.create_app()

    # Статистика
    resp_stats = await dispatch_request(app, "GET", "/pftb/api/v1/stats")
    assert resp_stats.status == 200
    stats = json.loads(resp_stats.text)["stats"]
    assert stats["total"] >= 1
    assert stats["active"] >= 1

    # Детали существующей задачи
    resp_detail = await dispatch_request(app, "GET", f"/pftb/api/v1/tasks/{t_id}")
    assert resp_detail.status == 200
    task = json.loads(resp_detail.text)["task"]
    assert task["id"] == t_id
    assert task["title"] == "Тест детали"
    assert task["description"] == "Подробное описание"

    # Детали несуществующей задачи -> 404
    resp_404 = await dispatch_request(app, "GET", "/pftb/api/v1/tasks/999999")
    assert resp_404.status == 404


@pytest.mark.asyncio
async def test_api_complete_task(initialized_db):
    """Проверка закрытия задачи через Web API (кнопка Завершить)."""
    t_id = await create_task("Задача для завершения", db_path=initialized_db)

    server = WebServer(prefix="/pftb", db_path=initialized_db)
    app = server.create_app()

    # Завершаем задачу
    resp = await dispatch_request(app, "POST", f"/pftb/api/v1/tasks/{t_id}/complete")
    assert resp.status == 200
    data = json.loads(resp.text)
    assert data["success"] is True
    assert data["task"]["status"] == "completed"

    # Проверяем в БД
    fresh = await get_task_by_id(t_id, db_path=initialized_db)
    assert fresh["status"] == "completed"

    # Повторный вызов безопасен (already_completed)
    resp_repeat = await dispatch_request(app, "POST", f"/pftb/api/v1/tasks/{t_id}/complete")
    assert resp_repeat.status == 200
    assert json.loads(resp_repeat.text)["already_completed"] is True


@pytest.mark.asyncio
async def test_api_to_calendar_with_and_without_deadline(initialized_db):
    """
    Проверка кнопки «В календарь»:
    - Если у задачи ЕСТЬ дедлайн — успешно синхронизируется с календарем.
    - Если у задачи НЕТ дедлайна — возвращается HTTP 400 с ошибкой (строгая аналогия с Telegram).
    """
    # 1. Задача БЕЗ дедлайна
    t_no_deadline = await create_task("Задача без дедлайна", db_path=initialized_db)

    server = WebServer(prefix="/pftb", db_path=initialized_db)
    app = server.create_app()

    resp_fail = await dispatch_request(app, "POST", f"/pftb/api/v1/tasks/{t_no_deadline}/to_calendar")
    assert resp_fail.status == 400
    data_fail = json.loads(resp_fail.text)
    assert data_fail["success"] is False
    assert "нет дедлайна" in data_fail["error"]

    # 2. Задача С дедлайном
    t_with_deadline = await create_task(
        "Задача с дедлайном",
        deadline="2026-11-26 19:00:00",
        db_path=initialized_db,
    )

    mock_cal = MagicMock()
    mock_cal.is_enabled = True
    mock_cal.create_event = AsyncMock(return_value={"uid": "cal_test_uid_99", "url": "https://cal.test/99"})

    with patch("bot.calendar_service", mock_cal):
        resp_ok = await dispatch_request(app, "POST", f"/pftb/api/v1/tasks/{t_with_deadline}/to_calendar")
        assert resp_ok.status == 200
        data_ok = json.loads(resp_ok.text)
        assert data_ok["success"] is True
        assert data_ok["task"]["caldav_uid"] == "cal_test_uid_99"
        assert data_ok["task"]["category"] == "calendar"

    # Проверяем в БД
    fresh = await get_task_by_id(t_with_deadline, db_path=initialized_db)
    assert fresh["caldav_uid"] == "cal_test_uid_99"
    assert fresh["category"] == "calendar"


@pytest.mark.asyncio
async def test_trello_webhook_via_webserver():
    """Проверка обработки вебхуков Trello через единый WebServer."""
    handled_events = []

    async def mock_handler(event):
        handled_events.append(event)

    server = WebServer(prefix="/pftb", trello_event_handler=mock_handler)
    app = server.create_app()

    # 1. HEAD проверка на /pftb/trello/webhook
    resp_head = await dispatch_request(app, "HEAD", "/pftb/trello/webhook")
    assert resp_head.status == 200

    # 2. GET healthcheck на /pftb/trello/webhook
    resp_get = await dispatch_request(app, "GET", "/pftb/trello/webhook")
    assert resp_get.status == 200
    assert "PFTB Trello Webhook Endpoint" in resp_get.text

    # 3. POST событие от Trello на /pftb/trello/webhook
    payload = {"action": {"type": "updateCard", "data": {"card": {"id": "c1", "name": "Купить молоко"}}}}
    resp_post = await dispatch_request(app, "POST", "/pftb/trello/webhook", json_data=payload)
    assert resp_post.status == 200

    await asyncio.sleep(0.05)
    assert len(handled_events) == 1
    assert handled_events[0]["action"]["type"] == "updateCard"

    # 4. Обратная совместимость с корневым /trello/webhook
    resp_legacy = await dispatch_request(app, "HEAD", "/trello/webhook")
    assert resp_legacy.status == 200


def test_enrich_task_fields_trello_support():
    """Проверка вычисления полей доступности кнопки 'В Trello'."""
    # Задача без Trello (не завершена) -> can_add_to_trello = True
    t1 = {"id": 1, "status": "pending", "trello_card_id": None}
    e1 = enrich_task_fields(t1)
    assert e1["is_in_trello"] is False
    assert e1["can_add_to_trello"] is True

    # Задача уже привязана к Trello -> can_add_to_trello = False, is_in_trello = True
    t2 = {"id": 2, "status": "pending", "trello_card_id": "card_abc"}
    e2 = enrich_task_fields(t2)
    assert e2["is_in_trello"] is True
    assert e2["can_add_to_trello"] is False

    # Задача завершена (хоть и без Trello) -> can_add_to_trello = False
    t3 = {"id": 3, "status": "completed", "trello_card_id": None}
    e3 = enrich_task_fields(t3)
    assert e3["is_in_trello"] is False
    assert e3["can_add_to_trello"] is False


def test_web_ui_html_rendering_trello_elements():
    """Проверка наличия кнопки 'В Trello', кнопки 'Sync Trello' и их стилей в HTML."""
    html = render_html_page(prefix="/pftb", api_prefix="/pftb/api/v1")
    assert "btn-trello" in html
    assert "В Trello" in html
    assert "Sync Trello" in html
    assert "handleAddToTrello" in html
    assert "handleSyncTrello" in html


@pytest.mark.asyncio
async def test_api_add_to_trello(initialized_db):
    """Проверка добавления задачи в Trello через Web API (кнопка 'В Trello')."""
    t_id = await create_task(
        "Задача для экспорта в Trello",
        description="Купить кофе в зернах",
        db_path=initialized_db,
    )

    server = WebServer(prefix="/pftb", db_path=initialized_db)
    app = server.create_app()

    mock_trello = MagicMock()
    mock_trello.is_enabled = True
    mock_trello.sync_task_created = AsyncMock(return_value={
        "card_id": "card_test_trello_111",
        "card_url": "https://trello.com/c/test111",
    })

    with patch("bot.trello_service", mock_trello):
        resp = await dispatch_request(app, "POST", f"/pftb/api/v1/tasks/{t_id}/to_trello")
        assert resp.status == 200
        data = json.loads(resp.text)
        assert data["success"] is True
        assert data["task"]["trello_card_id"] == "card_test_trello_111"
        assert data["task"]["trello_card_url"] == "https://trello.com/c/test111"
        assert data["task"]["is_in_trello"] is True
        assert data["task"]["can_add_to_trello"] is False

    # Проверяем в БД
    fresh = await get_task_by_id(t_id, db_path=initialized_db)
    assert fresh["trello_card_id"] == "card_test_trello_111"
    assert fresh["trello_card_url"] == "https://trello.com/c/test111"

    # Повторный вызов -> already_in_trello
    with patch("bot.trello_service", mock_trello):
        resp_repeat = await dispatch_request(app, "POST", f"/pftb/api/v1/tasks/{t_id}/to_trello")
        assert resp_repeat.status == 200
        data_repeat = json.loads(resp_repeat.text)
        assert data_repeat["success"] is True
        assert data_repeat["already_in_trello"] is True


@pytest.mark.asyncio
async def test_api_add_to_trello_disabled(initialized_db):
    """Проверка ошибки при экспорте в Trello, если сервис отключен."""
    t_id = await create_task("Задача без Trello", db_path=initialized_db)

    server = WebServer(prefix="/pftb", db_path=initialized_db)
    app = server.create_app()

    mock_trello = MagicMock()
    mock_trello.is_enabled = False

    with patch("bot.trello_service", mock_trello):
        resp = await dispatch_request(app, "POST", f"/pftb/api/v1/tasks/{t_id}/to_trello")
        assert resp.status == 400
        data = json.loads(resp.text)
        assert data["success"] is False
        assert "не включена" in data["error"]


@pytest.mark.asyncio
async def test_api_sync_trello_endpoint(initialized_db):
    """Проверка эндпоинта ручной синхронизации /pftb/api/v1/trello/sync."""
    server = WebServer(prefix="/pftb", db_path=initialized_db)
    app = server.create_app()

    mock_trello = MagicMock()
    mock_trello.is_enabled = True
    mock_trello.poll_all_and_sync = AsyncMock(return_value=[
        {"id": "new_c1", "name": "Новая задача из Trello"}
    ])

    with patch("bot.trello_service", mock_trello):
        resp = await dispatch_request(app, "POST", "/pftb/api/v1/trello/sync")
        assert resp.status == 200
        data = json.loads(resp.text)
        assert data["success"] is True
        assert data["new_tasks_count"] == 1
        assert "Синхронизация с Trello завершена" in data["message"]
