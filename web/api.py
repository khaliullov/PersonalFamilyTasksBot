import json
import logging
import math
from typing import Any, Dict, Optional

from aiohttp import web

from database import (
    _get_db_path,
    get_task_by_id,
    get_task_stats,
    get_tasks,
)
from web.config import API_PREFIX_KEY, API_V1_PREFIX, DB_PATH_KEY, PREFIX_KEY, WEB_PREFIX

logger = logging.getLogger("FamilyTasksBot.WebAPI")


def enrich_task_fields(task: Dict[str, Any]) -> Dict[str, Any]:
    """Добавляет вспомогательные вычисляемые поля для Web UI."""
    task_copy = dict(task)
    has_deadline = bool(task_copy.get("deadline"))
    is_in_calendar = bool(task_copy.get("caldav_uid"))
    is_completed = task_copy.get("status") == "completed"

    task_copy["has_deadline"] = has_deadline
    task_copy["is_in_calendar"] = is_in_calendar
    task_copy["is_completed"] = is_completed
    task_copy["can_complete"] = not is_completed
    # Кнопка 'В календарь' доступна ТОЛЬКО если есть дедлайн, нет caldav_uid и задача не завершена
    task_copy["can_add_to_calendar"] = has_deadline and not is_in_calendar and not is_completed
    return task_copy


def _get_app_db_path(request: web.Request) -> Optional[str]:
    """Извлекает путь к БД из настроек приложения."""
    return request.app.get(DB_PATH_KEY) or request.app.get("db_path")


async def api_health(request: web.Request) -> web.Response:
    """Проверка состояния сервиса."""
    prefix = request.app.get(PREFIX_KEY) or request.app.get("prefix", WEB_PREFIX)
    api_prefix = request.app.get(API_PREFIX_KEY) or request.app.get("api_prefix", API_V1_PREFIX)
    return web.json_response({
        "status": "ok",
        "prefix": prefix,
        "api_prefix": api_prefix,
    })


async def api_get_stats(request: web.Request) -> web.Response:
    """Агрегированная статистика по всем задачам."""
    db_path = _get_app_db_path(request)
    try:
        stats = await get_task_stats(db_path=db_path)
        return web.json_response({"success": True, "stats": stats})
    except Exception as e:
        logger.exception("Ошибка при получении статистики задач: %s", e)
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_get_tasks(request: web.Request) -> web.Response:
    """
    Список задач с пагинацией, фильтрацией по статусу/категории и поиском.
    """
    db_path = _get_app_db_path(request)
    try:
        query = request.query
        page = max(1, int(query.get("page", 1)))
        per_page = max(1, min(int(query.get("per_page", 10)), 100))
        status = query.get("status", "all").strip()
        category = query.get("category", "all").strip()
        search = query.get("search", "").strip() or None
        sort_by = query.get("sort_by", "id").strip()
        sort_order = query.get("sort_order", "desc").strip()

        offset = (page - 1) * per_page
        tasks, total = await get_tasks(
            status=status if status != "all" else None,
            category=category if category != "all" else None,
            search=search,
            limit=per_page,
            offset=offset,
            sort_by=sort_by,
            sort_order=sort_order,
            db_path=db_path,
        )

        total_pages = math.ceil(total / per_page) if total > 0 else 1
        enriched_tasks = [enrich_task_fields(t) for t in tasks]

        return web.json_response({
            "success": True,
            "tasks": enriched_tasks,
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages,
                "has_prev": page > 1,
                "has_next": page < total_pages,
            },
        })
    except ValueError as e:
        return web.json_response({"success": False, "error": f"Некорректные параметры: {e}"}, status=400)
    except Exception as e:
        logger.exception("Ошибка при получении списка задач: %s", e)
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_get_task_detail(request: web.Request) -> web.Response:
    """Получение детальной информации по конкретной задаче."""
    db_path = _get_app_db_path(request)
    raw_id = request.match_info.get("id", "")
    if not raw_id.isdigit():
        return web.json_response({"success": False, "error": "Некорректный ID задачи"}, status=400)

    task_id = int(raw_id)
    try:
        task = await get_task_by_id(task_id, db_path=db_path)
        if not task:
            return web.json_response({"success": False, "error": f"Задача #{task_id} не найдена"}, status=404)
        return web.json_response({"success": True, "task": enrich_task_fields(task)})
    except Exception as e:
        logger.exception("Ошибка при получении деталей задачи #%d: %s", task_id, e)
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_complete_task(request: web.Request) -> web.Response:
    """Завершение задачи по ID."""
    db_path = _get_app_db_path(request)
    raw_id = request.match_info.get("id", "")
    if not raw_id.isdigit():
        return web.json_response({"success": False, "error": "Некорректный ID задачи"}, status=400)

    task_id = int(raw_id)
    try:
        from bot import complete_task_by_id
        task = await get_task_by_id(task_id, db_path=db_path)
        if not task:
            return web.json_response({"success": False, "error": f"Задача #{task_id} не найдена"}, status=404)

        if task.get("status") == "completed":
            return web.json_response({
                "success": True,
                "already_completed": True,
                "message": f"Задача #{task_id} уже была отмечена как выполненная.",
                "task": enrich_task_fields(task),
            })

        completed_task = await complete_task_by_id(task_id, task=task, db_path=db_path)
        # Получаем обновленную запись из БД
        fresh_task = await get_task_by_id(task_id, db_path=db_path)
        return web.json_response({
            "success": True,
            "message": f"✅ Задача #{task_id} успешно завершена!",
            "task": enrich_task_fields(fresh_task or completed_task or task),
        })
    except Exception as e:
        logger.exception("Ошибка при завершении задачи #%d: %s", task_id, e)
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_add_to_calendar(request: web.Request) -> web.Response:
    """
    Добавление задачи в календарь Apple (CalDAV).
    Аналогично поведению Telegram-бота: доступно только если у задачи есть дедлайн.
    """
    db_path = _get_app_db_path(request)
    raw_id = request.match_info.get("id", "")
    if not raw_id.isdigit():
        return web.json_response({"success": False, "error": "Некорректный ID задачи"}, status=400)

    task_id = int(raw_id)
    try:
        from bot import add_task_to_calendar
        # enforce_deadline=True: кнопка и API требуют дедлайн по аналогии с Telegram
        result = await add_task_to_calendar(task_id, db_path=db_path, enforce_deadline=True)
        status_code = result.get("status", 200)

        response_payload = {
            "success": result.get("success", False),
            "message": result.get("message") or result.get("error"),
        }
        if result.get("task"):
            response_payload["task"] = enrich_task_fields(result["task"])
        if result.get("error"):
            response_payload["error"] = result["error"]
        if result.get("already_in_calendar"):
            response_payload["already_in_calendar"] = True

        return web.json_response(response_payload, status=status_code)
    except Exception as e:
        logger.exception("Ошибка при добавлении задачи #%d в календарь: %s", task_id, e)
        return web.json_response({"success": False, "error": str(e)}, status=500)
