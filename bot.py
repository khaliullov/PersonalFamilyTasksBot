import asyncio
from datetime import datetime, time, timedelta
import html
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from voice.config import (
    ASR_DEVICE,
    ASR_ENABLED,
    ASR_MODEL_SIZE,
    VOICE_SUMMARY_MODE,
)
from voice.db import init_voice_db
from voice.processor import process_voice_message
from trello import (
    TRELLO_ENABLED,
    TrelloSyncService,
)
from calendar_sync import (
    CALDAV_ENABLED,
    CalendarSyncService,
)
from tz_utils import (
    BOT_TIMEZONE,
    TIMEZONE_STR,
    ensure_tz,
    format_dt,
    get_now,
)

# ==============================================================================
# КОНФИГУРАЦИЯ И КОНСТАНТЫ
# ==============================================================================

# Токен бота (получен от @BotFather)
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")

# Telegram ID жены (только сообщения с этого ID будут анализироваться)
WIFE_ID: int = int(os.getenv("WIFE_ID", "123456789"))

# Telegram ID владельца бота (куда отправлять уведомления и напоминания)
MY_ID: int = int(os.getenv("MY_ID", "987654321"))

# Параметры локального Ollama API
_raw_ollama_url: str = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
if not _raw_ollama_url.rstrip("/").endswith("/api/generate"):
    OLLAMA_URL: str = f"{_raw_ollama_url.rstrip('/')}/api/generate"
else:
    OLLAMA_URL: str = _raw_ollama_url.rstrip("/")

# Если бот запущен внутри контейнера Docker, подменяем localhost на host.docker.internal
if (os.path.exists("/.dockerenv") or os.getenv("IN_DOCKER") == "1") and ("localhost" in OLLAMA_URL or "127.0.0.1" in OLLAMA_URL):
    OLLAMA_URL = OLLAMA_URL.replace("localhost", "host.docker.internal").replace("127.0.0.1", "host.docker.internal")

OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen3.8:27b-mlx")

# Путь к локальной базе данных SQLite
DB_PATH: str = os.getenv("DB_PATH", "tasks.db")

# Таймаут запроса к Ollama в секундах (для 27B модели на MLX)
OLLAMA_TIMEOUT_SECONDS: int = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "120"))

# Задержка дебаунса (секунды): объединяет быстро следующие друг за другом сообщения
DEBOUNCE_DELAY_SECONDS: float = float(os.getenv("DEBOUNCE_DELAY_SECONDS", "3.5"))

# Размер контекстного окна (количество последних сообщений для анализа)
CONTEXT_HISTORY_LIMIT: int = int(os.getenv("CONTEXT_HISTORY_LIMIT", "7"))

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("FamilyTasksBot")

# Глобальный экземпляр бота (для асинхронных уведомлений из вебхуков/фоновых задач)
_current_bot: Optional[Bot] = None

# Сервис синхронизации задач с Trello
trello_service = TrelloSyncService()

# Сервис синхронизации событий с Apple Calendar (CalDAV)
calendar_service = CalendarSyncService()

# ==============================================================================
# РАБОТА С БАЗОЙ ДАННЫХ (PostgreSQL / SQLite)
# ==============================================================================

from database import (
    _get_db_path,
    close_pg_pool,
    create_task,
    find_active_task_for_trello_card,
    get_active_tasks,
    get_latest_active_task,
    get_pending_tasks_with_deadline,
    get_recent_chat_history,
    get_task_by_id,
    get_task_by_trello_card_id,
    init_db,
    save_chat_message,
    update_task,
    update_task_caldav_info,
    update_task_status,
    update_task_trello_info,
)


# ==============================================================================
# КОНТЕКСТНЫЙ СИСТЕМНЫЙ ПРОМПТ И ВЗАИМОДЕЙСТВИЕ С OLLAMA
# ==============================================================================


def build_prompt(
    recent_messages: List[Dict[str, Any]],
    latest_task: Optional[Dict[str, Any]],
    current_dt: datetime,
) -> str:
    """
    Формирует контекстный системный промпт для LLM:
    - Передает последние сообщения чата (включая ссылки и реплики).
    - Передает последнюю активную задачу (для возможности ее обновления/дополнения).
    - Динамически подставляет текущую дату и день недели для дедлайнов.
    """
    weekdays_ru = [
        "понедельник",
        "вторник",
        "среда",
        "четверг",
        "пятница",
        "суббота",
        "воскресенье",
    ]
    current_str = current_dt.strftime("%Y-%m-%d %H:%M:%S")
    weekday_str = weekdays_ru[current_dt.weekday()]

    # Формируем блок истории переписки
    history_lines = []
    for msg in recent_messages:
        ts = msg.get("created_at", "")
        sender = msg.get("sender_name", "Собеседник")
        text = msg.get("text", "").strip()
        history_lines.append(f"[{ts}] {sender}: {text}")
    history_text = "\n".join(history_lines) if history_lines else "(история сообщений пуста)"

    # Формируем блок последней задачи
    if latest_task:
        task_info = (
            f"ID #{latest_task['id']} | Название: \"{latest_task['title']}\"\n"
            f"Текущие детали: \"{latest_task.get('description') or 'нет'}\"\n"
            f"Дедлайн: {latest_task.get('deadline') or 'не установлен'}\n"
            f"Создана: {latest_task['created_at']}"
        )
    else:
        task_info = "Активных недавних задач нет."

    return f"""Ты — интеллектуальный ассистент мужа. Твоя задача — анализировать сообщения от его жены и аккуратно управлять семейными задачами.

Текущая дата и время сервера: {current_str} ({weekday_str}).
Используй текущую дату для вычисления точных относительных дедлайнов ("сегодня", "завтра к 15:00", "в пятницу", "послезавтра", "через 2 часа" и т.д.).
Если указан день без конкретного часа, устанавливай время по умолчанию на 18:00:00.

---
ИСТОРИЯ ПОСЛЕДНИХ СООБЩЕНИЙ (в хронологическом порядке):
{history_text}
---
ПОСЛЕДНЯЯ СОЗДАННАЯ АКТИВНАЯ ЗАДАЧА В СИСТЕМЕ:
{task_info}
---

ПРАВИЛА АНАЛИЗА:
1. ДЕЙСТВИЕ "create" (создать новую задачу или событие календаря):
   - Выбирай, если в последних сообщениях появилось новое поручение, просьба что-то сделать, купить, забрать, выбрать, оплатить или встретиться/посетить врача/созвониться.
   - СВЯЗКА КОНТЕКСТА (Ссылки ДО задачи): Если жена сначала прислала ссылки/фото/товары, а затем написала просьбу (например: «выбери игрушку для сына»), ты ОБЯЗАН объединить это:
     * "task_title": краткая и ясная суть задачи (например: "Выбрать игрушку для сына на день рождения")
     * "task_description": собери все ссылки, наименования и детали из предыдущих сообщений в связный список.

2. ДЕЙСТВИЕ "update" (дополнить/отредактировать существующую задачу):
   - Выбирай, если задача УЖЕ была создана ранее (см. "ПОСЛЕДНЯЯ СОЗДАННАЯ АКТИВНАЯ ЗАДАЧА"), а в самых свежих сообщениях жена присылает дополнительные ссылки, уточнения, изменения сроков или детали к ней.
   - Укажи "task_id" этой задачи, а в "task_description" приведи новые/дополненные детали и ссылки.

3. ДЕЙСТВИЕ "none" (задача не требуется):
   - Обычный разговор, эмоции, благодарность ("спасибо!", "люблю"), вопрос без поручения ("ты где?"), мем или приветствие.

КАТЕГОРИИ (поле "category"):
- "calendar": Любые события, поездки, встречи, приемы врачей/специалистов, записи, процедуры, видеосозвоны или поручения, привязанные к ТОЧНОМУ ВРЕМЕНИ ДНЯ или расписанию (например: «Отвезти Лену в 19:30 и взять Данию с собой», «Забрать ребенка в 18:00», «Идем к остеопату 25 ноября в 9:00», «Завтра созвон в Zoom в 15:00 https://zoom.us/...», «Запись к стоматологу в пятницу в 11:30», «Выехать в аэропорт в 06:30»).
  Если действие или поездка привязаны к конкретному часу/минутам встречи или выезда — это ВСЕГДА "calendar".
  Для таких событий ОБЯЗАТЕЛЬНО укажи deadline_timestamp (время начала события), а также при наличии в тексте:
  * "location": место встречи или точный адрес (например: "ул. Пушкина 10", "Клиника Остеомед", null если не указано)
  * "meeting_url": ссылка для онлайн-встречи (Zoom, Google Meet, Яндекс Телемост и т.п., null если не указано)
  * "contact_info": контактное лицо и/или номер телефона (например: "доктор Иванова +79991234567", null если не указано)
- "task": Обычные бытовые дела, поручения, покупки или задачи без привязки к конкретному часу встречи/выезда (например: «Купить молоко», «Забрать посылку из СДЭК до конца недели», «Выбрать подарок сыну»).

ТРЕБОВАНИЯ К ФОРМАТУ ОТВЕТА:
Строго верни валидный JSON без markdown-разметки и вводных слов по схеме:
{{
  "action": "create" | "update" | "none",
  "is_task": true/false,
  "category": "task" | "calendar",
  "task_id": 123 (число ID задачи при action="update", иначе null),
  "task_title": "Суть задачи или название события коротко и ясно" (или null),
  "task_description": "Ссылки, варианты и пояснения" (или null),
  "has_deadline": true/false,
  "deadline_timestamp": "YYYY-MM-DD HH:MM:SS" (или null),
  "location": "Место встречи или адрес" (или null),
  "meeting_url": "URL для видеосозвона" (или null),
  "contact_info": "Контакт и/или телефон" (или null)
}}
"""


def extract_and_parse_json(raw_response: str) -> Dict[str, Any]:
    """
    Программно очищает ответ модели от тегов рассуждений (<thought>...</thought>, <think>...</think>),
    markdown-разметки и извлекает строго валидный JSON.
    """
    # 1. Удаление закрытых тегов рассуждений
    cleaned = re.sub(
        r"<thought[^>]*>.*?</thought>",
        "",
        raw_response,
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = re.sub(
        r"<think[^>]*>.*?</think>",
        "",
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # 2. Удаление незакрытых тегов рассуждений (при обрыве генерации)
    cleaned = re.sub(
        r"<thought[^>]*>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE
    )
    cleaned = re.sub(
        r"<think[^>]*>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE
    )

    # 3. Удаление Markdown-блоков кода
    cleaned = re.sub(r"```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()

    # 4. Поиск границ первого валидного JSON-объекта {...}
    start_idx = cleaned.find("{")
    end_idx = cleaned.rfind("}")

    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        raise ValueError(f"В ответе модели не найден JSON-объект: {raw_response[:200]}")

    json_str = cleaned[start_idx : end_idx + 1].strip()
    data = json.loads(json_str)

    if not isinstance(data, dict):
        raise ValueError("Распарсенный JSON не является словарем.")

    data.setdefault("category", "task")
    data.setdefault("location", None)
    data.setdefault("meeting_url", None)
    data.setdefault("contact_info", None)

    return data


async def analyze_context_with_ollama(
    recent_messages: List[Dict[str, Any]],
    latest_task: Optional[Dict[str, Any]],
    ollama_url: str = OLLAMA_URL,
    model: str = OLLAMA_MODEL,
) -> Optional[Dict[str, Any]]:
    """
    Отправляет контекстный запрос в Ollama API и возвращает структурированный ответ.
    """
    current_dt = get_now()
    prompt = build_prompt(
        recent_messages=recent_messages,
        latest_task=latest_task,
        current_dt=current_dt,
    )

    # ВАЖНО: Не передаем "format": "json", так как MLX-раннеры в Ollama не поддерживают
    # constrained decoding / structured output и возвращают ошибку HTTP 501:
    # {"error":"structured output is unavailable"}.
    # Модель строго инструктирована системным промптом, а extract_and_parse_json парсит JSON.
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }

    logger.info(
        "Отправка контекста (%d сообщений) в Ollama (%s) [%s]...",
        len(recent_messages),
        model,
        ollama_url,
    )

    timeout = aiohttp.ClientTimeout(total=OLLAMA_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(ollama_url, json=payload) as response:
                if response.status != 200:
                    err_text = await response.text()
                    logger.error(
                        "Ollama вернул ошибку HTTP %d: %s",
                        response.status,
                        err_text,
                    )
                    return None

                response_data = await response.json()
                raw_response = response_data.get("response", "")

        logger.debug("Сырой ответ от Ollama:\n%s", raw_response)
        parsed_result = extract_and_parse_json(raw_response)
        return parsed_result

    except asyncio.TimeoutError:
        logger.error(
            "Таймаут обращения к Ollama (%d с). Модель отвечает слишком долго.",
            OLLAMA_TIMEOUT_SECONDS,
        )
        return None
    except aiohttp.ClientError as e:
        logger.error("Сетевая ошибка при запросе к Ollama: %s", e)
        return None
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("Ошибка при разборе JSON ответа Ollama: %s", e)
        return None
    except Exception as e:
        logger.exception("Непредвиденное исключение при анализе текста: %s", e)
        return None


def parse_deadline(
    deadline_str: Optional[str],
    tz: Optional[Any] = None,
) -> Optional[datetime]:
    """
    Парсит строку даты/времени в объект datetime с поддержкой нескольких форматов
    и привязывает настроенный часовой пояс бота (по умолчанию BOT_TIMEZONE).
    """
    target_tz = tz or BOT_TIMEZONE
    if not deadline_str or not isinstance(deadline_str, str):
        return None

    deadline_str = deadline_str.strip()

    # 1. Попытка распарсить ISO формат (например, 2026-11-25T09:00:00+04:00 или с Z)
    try:
        iso_str = deadline_str.replace("Z", "+00:00")
        dt_iso = datetime.fromisoformat(iso_str)
        if dt_iso.tzinfo is not None:
            return dt_iso.astimezone(target_tz)
        return dt_iso.replace(tzinfo=target_tz)
    except Exception:
        pass

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(deadline_str, fmt)
            if fmt == "%Y-%m-%d":
                dt = dt.replace(hour=18, minute=0, second=0)
            return dt.replace(tzinfo=target_tz)
        except ValueError:
            continue

    logger.warning("Не удалось распарсить дедлайн '%s'.", deadline_str)
    return None


# ==============================================================================
# ПЛАНИРОВЩИК НАПОМИНАНИЙ (APScheduler)
# ==============================================================================

scheduler = AsyncIOScheduler(timezone=BOT_TIMEZONE)

_MONTHS_MAP: Dict[str, int] = {
    "янв": 1, "января": 1, "январе": 1,
    "фев": 2, "февраля": 2, "феврале": 2,
    "мар": 3, "марта": 3, "марте": 3,
    "апр": 4, "апреля": 4, "апреле": 4,
    "май": 5, "мая": 5, "мае": 5,
    "июн": 6, "июня": 6, "июне": 6,
    "июл": 7, "июля": 7, "июле": 7,
    "авг": 8, "августа": 8, "августе": 8,
    "сен": 9, "сентября": 9, "сентябре": 9,
    "окт": 10, "октября": 10, "октябре": 10,
    "ноя": 11, "ноября": 11, "ноябре": 11,
    "дек": 12, "декабря": 12, "декабре": 12,
}


def resolve_event_datetime(
    task: Dict[str, Any],
    tz: Optional[Any] = None,
) -> datetime:
    """
    Определяет дату и время события для календаря:
    1. Если у задачи уже есть deadline — парсит его с привязкой к часовому поясу бота.
    2. Если дедлайна нет — пытается извлечь время и дату из заголовка или описания задачи.
    3. При отсутствии временных ориентиров берет ближайший круглый час.
    """
    target_tz = tz or BOT_TIMEZONE
    if task.get("deadline"):
        dt = parse_deadline(task["deadline"], tz=target_tz)
        if dt:
            return dt

    now = get_now()
    text = f"{task.get('title', '')} {task.get('description', '')}".strip()
    text_lower = text.lower()

    # 1. Смещение по ключевым словам
    days_ahead = 0
    if "послезавтра" in text_lower:
        days_ahead = 2
    elif "завтра" in text_lower:
        days_ahead = 1

    # 2. Поиск даты (например, "25 ноября" или "25.11")
    target_day: Optional[int] = None
    target_month: Optional[int] = None
    target_year: int = now.year

    date_month_match = re.search(
        r"\b(\d{1,2})\s+(январ[яе]|феврал[яе]|март[ае]|апрел[яе]|ма[яе]|июн[яе]|июл[яе]|август[ае]|сентябр[яе]|октябр[яе]|ноябр[яе]|декабр[яе])\b",
        text_lower,
    )
    if date_month_match:
        target_day = int(date_month_match.group(1))
        m_word = date_month_match.group(2)
        target_month = _MONTHS_MAP.get(m_word, _MONTHS_MAP.get(m_word[:3]))
        if target_month and target_month < now.month:
            target_year += 1
    else:
        date_dot_match = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b", text_lower)
        if date_dot_match:
            target_day = int(date_dot_match.group(1))
            target_month = int(date_dot_match.group(2))
            if date_dot_match.group(3):
                y = int(date_dot_match.group(3))
                target_year = y if y > 100 else 2000 + y
            elif target_month < now.month:
                target_year += 1

    # 3. Поиск времени (например: "в 19:30", "в 19.30", "в 9:00", "в 18")
    target_hour: Optional[int] = None
    target_minute: int = 0

    time_match = re.search(
        r"(?:^|[^\d])(?:в|к|на|at)\s*(\d{1,2})(?:[:.](\d{2}))?(?:\s*(утра|вечера|дня))?\b",
        text_lower,
    )
    if time_match:
        target_hour = int(time_match.group(1))
        if time_match.group(2):
            target_minute = int(time_match.group(2))
        tod = time_match.group(3)
        if tod == "вечера" and target_hour < 12:
            target_hour += 12
        elif tod == "дня" and target_hour < 12 and target_hour != 12:
            target_hour += 12
        elif tod == "утра" and target_hour == 12:
            target_hour = 0

    # 4. Сборка итогового datetime
    if target_day and target_month:
        hour = target_hour if target_hour is not None else 10
        minute = target_minute
        try:
            return datetime(target_year, target_month, target_day, hour, minute, tzinfo=target_tz)
        except ValueError:
            pass

    if target_hour is not None:
        target_date = (now + timedelta(days=days_ahead)).date()
        candidate = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            target_hour,
            target_minute,
            tzinfo=target_tz,
        )
        if days_ahead == 0 and candidate < now - timedelta(hours=2):
            candidate += timedelta(days=1)
        return candidate

    # 5. Резервный вариант: следующий круглый час
    return (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def get_task_inline_keyboard(
    task_id: int,
    show_calendar_button: bool = True,
    is_in_calendar: bool = False,
    has_deadline: bool = True,
) -> types.InlineKeyboardMarkup:
    """
    Формирует inline клавиатуру с кнопкой быстрого завершения задачи
    и (при наличии явного дедлайна) кнопкой добавления в календарь.
    """
    buttons = [
        types.InlineKeyboardButton(
            text="✅ Завершить",
            callback_data=f"done:{task_id}",
        )
    ]
    if is_in_calendar:
        buttons.append(
            types.InlineKeyboardButton(
                text="🗓 В календаре",
                callback_data="noop_cal",
            )
        )
    elif show_calendar_button and has_deadline:
        buttons.append(
            types.InlineKeyboardButton(
                text="📅 В календарь",
                callback_data=f"to_calendar:{task_id}",
            )
        )
    return types.InlineKeyboardMarkup(inline_keyboard=[buttons])


async def send_task_reminder(
    bot: Bot,
    task_id: int,
    title: str,
    deadline_str: str,
    my_id: int = MY_ID,
    db_path: Optional[str] = None,
) -> None:
    """
    Callback-функция для APScheduler: отправляет уведомление пользователю MY_ID,
    когда наступает дедлайн задачи, и обновляет статус задачи в базе данных.
    Использует самое актуальное название задачи из базы данных (с учетом правок в Trello)
    и прикрепляет inline кнопку завершения.
    """
    target_path = _get_db_path(db_path)
    logger.info("Срабатывание напоминания по дедлайну для задачи #%d ('%s')", task_id, title)
    task = await get_task_by_id(task_id, db_path=target_path)

    # Используем самое актуальное название и поля из базы данных!
    actual_title = task.get("title") if task else title
    actual_deadline = task.get("deadline") if task else deadline_str
    description = task.get("description") if task else None
    category = task.get("category", "task") if task else "task"
    location = task.get("location") if task else None
    meeting_url = task.get("meeting_url") if task else None
    contact_info = task.get("contact_info") if task else None
    trello_card_url = task.get("trello_card_url") if task else None

    loc_block = f"\n📍 <b>Место:</b> {html.escape(location)}" if location else ""
    contact_block = f"\n📞 <b>Контакт:</b> {html.escape(contact_info)}" if contact_info else ""
    url_block = f"\n🌐 <b>Ссылка для созвона:</b> {html.escape(meeting_url)}" if meeting_url else ""
    desc_block = f"\n🔗 <b>Детали:</b>\n{html.escape(description)}\n" if description else ""
    trello_block = f"\n📋 <a href=\"{trello_card_url}\">Карточка в Trello</a>\n" if trello_card_url else ""

    header = "⏰ <b>НАПОМИНАНИЕ О СОБЫТИИ!</b>" if category == "calendar" else "⏰ <b>НАПОМИНАНИЕ О ДЕДЛАЙНЕ!</b>"
    item_label = "Событие" if category == "calendar" else "Задача"

    message_text = (
        f"{header}\n\n"
        f"📌 <b>{item_label}:</b> {html.escape(actual_title)}"
        f"{loc_block}"
        f"{contact_block}"
        f"{url_block}"
        f"{desc_block}\n"
        f"⏳ <b>Дедлайн:</b> <code>{actual_deadline}</code>\n"
        f"{trello_block}\n"
        f"<i>#{task_id} ожидает выполнения. Для завершения нажмите кнопку ниже:</i>"
    )

    in_cal = bool(task.get("caldav_uid")) if task else False
    has_dl = bool(actual_deadline)
    try:
        await bot.send_message(
            chat_id=my_id,
            text=message_text,
            reply_markup=get_task_inline_keyboard(
                task_id,
                show_calendar_button=not in_cal,
                is_in_calendar=in_cal,
                has_deadline=has_dl,
            ),
        )
        await update_task_status(task_id, "reminded", target_path)
    except Exception as e:
        logger.error("Ошибка при отправке напоминания в Telegram (chat_id=%d): %s", my_id, e)


async def schedule_task_job(
    scheduler_instance: AsyncIOScheduler,
    bot: Bot,
    task_id: int,
    title: str,
    deadline_dt: datetime,
    deadline_str: str,
) -> bool:
    """Регистрирует задачу в APScheduler, если дедлайн находится в будущем."""
    actual_deadline_dt = ensure_tz(deadline_dt)
    now = get_now()
    if actual_deadline_dt <= now:
        logger.warning(
            "Дедлайн задачи #%d (%s) уже в прошлом (сейчас %s). Таймер не запущен.",
            task_id,
            deadline_str,
            now.strftime("%Y-%m-%d %H:%M:%S"),
        )
        return False

    job_id = f"task_reminder_{task_id}"
    scheduler_instance.add_job(
        send_task_reminder,
        trigger=DateTrigger(run_date=actual_deadline_dt, timezone=BOT_TIMEZONE),
        args=[bot, task_id, title, deadline_str],
        id=job_id,
        replace_existing=True,
    )
    logger.info(
        "Запланировано напоминание для задачи #%d на %s (job_id='%s').",
        task_id,
        deadline_str,
        job_id,
    )
    return True


async def restore_scheduled_jobs(
    bot: Bot,
    scheduler_instance: AsyncIOScheduler,
    db_path: Optional[str] = None,
) -> None:
    """
    Восстанавливает активные напоминания из SQLite при перезапуске бота.
    """
    target_path = _get_db_path(db_path)
    pending_tasks = await get_pending_tasks_with_deadline(target_path)
    restored_count = 0
    now = get_now()

    for task in pending_tasks:
        task_id = task["id"]
        title = task["title"]
        deadline_str = task["deadline"]

        dt = parse_deadline(deadline_str)
        if dt and dt > now:
            await schedule_task_job(
                scheduler_instance=scheduler_instance,
                bot=bot,
                task_id=task_id,
                title=title,
                deadline_dt=dt,
                deadline_str=deadline_str,
            )
            restored_count += 1
        elif dt and dt <= now:
            logger.info(
                "Дедлайн задачи #%d истек, пока бот был оффлайн. Пропуск таймера.",
                task_id,
            )

    logger.info("Восстановлено напоминаний из базы данных: %d.", restored_count)


# ==============================================================================
# ОБРАБОТКА ДИАЛОГА С ДЕБАУНСОМ (ДЕБАТЧИНГ БЫСТРЫХ СООБЩЕНИЙ)
# ==============================================================================

# Хранилище фоновых тасок дебаунса для каждого отправителя
_debounce_tasks: Dict[int, asyncio.Task] = {}
_processing_lock = asyncio.Lock()


async def process_recent_context_with_ollama(bot: Bot, user_id: int) -> None:
    """
    Выполняет извлечение контекста, обращение к Ollama и создание/обновление задачи.
    Вызывается после завершения таймера дебаунса.
    """
    logger.info("Запуск контекстного анализа для пользователя %d...", user_id)

    # Получаем историю сообщений и последнюю задачу
    recent_messages = await get_recent_chat_history(limit=CONTEXT_HISTORY_LIMIT)
    latest_task = await get_latest_active_task()

    analysis = await analyze_context_with_ollama(
        recent_messages=recent_messages,
        latest_task=latest_task,
    )
    if not analysis:
        logger.warning("Анализ контекста не дал результатов или завершился с ошибкой.")
        return

    action = analysis.get("action", "none")
    is_task = analysis.get("is_task", False)

    # Если действие не определено явно, но is_task истинно
    if action == "none" and not is_task:
        logger.info("Сообщения классифицированы как обычный диалог (action='none').")
        return

    if action == "none" and is_task:
        action = "create"

    task_title = analysis.get("task_title")
    task_desc = analysis.get("task_description")
    has_deadline = bool(analysis.get("has_deadline", False))
    raw_deadline = analysis.get("deadline_timestamp")

    deadline_dt: Optional[datetime] = None
    deadline_str: Optional[str] = None
    if has_deadline and raw_deadline:
        deadline_dt = parse_deadline(raw_deadline)
        if deadline_dt:
            deadline_str = deadline_dt.strftime("%Y-%m-%d %H:%M:%S")
        else:
            has_deadline = False

    # --------------------------------------------------------------------------
    # СЦЕНАРИЙ 1: СОЗДАНИЕ НОВОЙ ЗАДАЧИ (action == "create")
    # --------------------------------------------------------------------------
    if action == "create":
        if not task_title:
            last_text = recent_messages[-1]["text"] if recent_messages else "Задача"
            task_title = last_text[:100]

        category = analysis.get("category") or "task"
        location = analysis.get("location")
        meeting_url = analysis.get("meeting_url")
        contact_info = analysis.get("contact_info")

        task_id = await create_task(
            title=task_title,
            description=task_desc,
            deadline=deadline_str,
            category=category,
            location=location,
            meeting_url=meeting_url,
            contact_info=contact_info,
        )
        logger.info(
            "Создана новая задача #%d: '%s' (Категория: %s, Дедлайн: %s)",
            task_id,
            task_title,
            category,
            deadline_str,
        )

        scheduled = False
        if has_deadline and deadline_dt:
            scheduled = await schedule_task_job(
                scheduler_instance=scheduler,
                bot=bot,
                task_id=task_id,
                title=task_title,
                deadline_dt=deadline_dt,
                deadline_str=deadline_str,
            )

        # Синхронизация с Apple Calendar (CalDAV) для категории calendar
        caldav_info = None
        if category == "calendar" and deadline_dt and calendar_service and calendar_service.is_enabled:
            try:
                caldav_info = await calendar_service.create_event(
                    title=task_title,
                    start_dt=deadline_dt,
                    location=location,
                    description=task_desc,
                    meeting_url=meeting_url,
                    contact_info=contact_info,
                )
                if caldav_info:
                    await update_task_caldav_info(
                        task_id=task_id,
                        caldav_uid=caldav_info["uid"],
                        caldav_url=caldav_info.get("url", ""),
                    )
            except Exception as e:
                logger.error("Ошибка синхронизации события #%d с CalDAV: %s", task_id, e)

        # Синхронизация с Trello (создание карточки в колонке Inbox)
        trello_card_url = None
        if trello_service and trello_service.is_enabled:
            try:
                card_data = await trello_service.sync_task_created(
                    task_id=task_id,
                    title=task_title,
                    description=task_desc,
                    deadline_dt=deadline_dt,
                )
                if card_data:
                    await update_task_trello_info(
                        task_id=task_id,
                        trello_card_id=card_data["card_id"],
                        trello_card_url=card_data.get("card_url", ""),
                    )
                    trello_card_url = card_data.get("card_url")
            except Exception as e:
                logger.error("Ошибка синхронизации созданной задачи #%d с Trello: %s", task_id, e)

        # Формируем уведомление для мужа
        if has_deadline and deadline_str:
            deadline_info = f"🕒 <code>{deadline_str}</code>"
            if scheduled:
                deadline_info += " <i>(напоминание запланировано)</i>"
            else:
                deadline_info += " ⚠️ <i>(дедлайн уже в прошлом!)</i>"
        else:
            deadline_info = "❌ Без дедлайна"

        desc_block = f"\n🔗 <b>Детали / ссылки:</b>\n{html.escape(task_desc)}\n" if task_desc else ""
        trello_block = f"\n📋 <a href=\"{trello_card_url}\">Открыть карточку в Trello</a>\n" if trello_card_url else ""
        loc_block = f"\n📍 <b>Место:</b> {html.escape(location)}" if location else ""
        phone_block = f"\n📞 <b>Контакт:</b> {html.escape(contact_info)}" if contact_info else ""
        url_block = f"\n🌐 <b>Ссылка для созвона:</b> {html.escape(meeting_url)}" if meeting_url else ""

        if category == "calendar":
            cal_status = (
                "✅ Добавлено в Apple Calendar"
                if caldav_info
                else (
                    "ℹ️ CalDAV не настроен (сохранено в боте)"
                    if not (calendar_service and calendar_service.is_enabled)
                    else "⚠️ Не удалось добавить в CalDAV"
                )
            )
            notification_text = (
                f"🗓 <b>Новое событие в календарь от жены!</b>\n\n"
                f"📌 <b>Событие:</b> {html.escape(task_title)}"
                f"{loc_block}"
                f"{phone_block}"
                f"{url_block}"
                f"{desc_block}\n"
                f"🕒 <b>Дата и время:</b> {deadline_info}\n"
                f"📅 <b>Календарь:</b> {cal_status}\n"
                f"{trello_block}\n"
                f"<i>Запись сохранена в базу (ID: #{task_id})</i>"
            )
        else:
            notification_text = (
                f"📥 <b>Новая задача от жены!</b>\n\n"
                f"📌 <b>Суть:</b> {html.escape(task_title)}"
                f"{desc_block}\n"
                f"📅 <b>Дедлайн:</b> {deadline_info}\n"
                f"{trello_block}\n"
                f"<i>Запись сохранена в базу (ID: #{task_id})</i>"
            )

        in_cal = bool(caldav_info)
        has_dl = bool(has_deadline and deadline_str)
        try:
            await bot.send_message(
                chat_id=MY_ID,
                text=notification_text,
                reply_markup=get_task_inline_keyboard(
                    task_id,
                    show_calendar_button=not in_cal,
                    is_in_calendar=in_cal,
                    has_deadline=has_dl,
                ),
            )
        except Exception as e:
            logger.error("Не удалось отправить уведомление о создании задачи в Telegram: %s", e)

    # --------------------------------------------------------------------------
    # СЦЕНАРИЙ 2: ОБНОВЛЕНИЕ / ДОПОЛНЕНИЕ СУЩЕСТВУЮЩЕЙ ЗАДАЧИ (action == "update")
    # --------------------------------------------------------------------------
    elif action == "update":
        target_task_id = analysis.get("task_id")
        if not target_task_id and latest_task:
            target_task_id = latest_task["id"]

        if not target_task_id:
            logger.warning("Модель вернула action='update', но task_id не найден. Пропуск.")
            return

        existing_task = await get_task_by_id(target_task_id)
        if not existing_task:
            logger.warning("Задача #%d для обновления не найдена в БД.", target_task_id)
            return

        # Аккуратно объединяем описание: сохраняем старое и добавляем новое, если они отличаются
        final_desc = task_desc
        if existing_task.get("description") and task_desc:
            old_d = existing_task["description"]
            if task_desc not in old_d and old_d not in task_desc:
                final_desc = f"{old_d}\n\n➕ <b>Дополнено:</b>\n{task_desc}"
            elif len(task_desc) > len(old_d):
                final_desc = task_desc
            else:
                final_desc = old_d
        elif existing_task.get("description") and not task_desc:
            final_desc = existing_task["description"]

        final_title = task_title if task_title else existing_task["title"]
        final_deadline = deadline_str if deadline_str else existing_task.get("deadline")
        final_loc = analysis.get("location") or existing_task.get("location")
        final_url = analysis.get("meeting_url") or existing_task.get("meeting_url")
        final_contact = analysis.get("contact_info") or existing_task.get("contact_info")

        await update_task(
            task_id=target_task_id,
            title=final_title,
            description=final_desc,
            deadline=final_deadline,
            location=final_loc,
            meeting_url=final_url,
            contact_info=final_contact,
        )
        logger.info("Задача #%d успешно обновлена новыми данными.", target_task_id)

        # Синхронизация изменений в карточку Trello
        if trello_service and trello_service.is_enabled and existing_task.get("trello_card_id"):
            try:
                await trello_service.sync_task_updated(
                    task_id=target_task_id,
                    trello_card_id=existing_task["trello_card_id"],
                    title=final_title,
                    description=final_desc,
                    deadline_dt=deadline_dt,
                )
            except Exception as e:
                logger.error("Ошибка обновления карточки Trello #%s: %s", existing_task["trello_card_id"], e)

        # Если дедлайн обновился — перепланируем напоминание в APScheduler
        if has_deadline and deadline_dt:
            await schedule_task_job(
                scheduler_instance=scheduler,
                bot=bot,
                task_id=target_task_id,
                title=final_title,
                deadline_dt=deadline_dt,
                deadline_str=deadline_str,
            )

        desc_block = f"\n🔗 <b>Обновленные детали / ссылки:</b>\n{html.escape(final_desc)}\n" if final_desc else ""
        dl_block = f"<code>{final_deadline}</code>" if final_deadline else "не указан"

        notification_text = (
            f"✏️ <b>Задача #{target_task_id} обновлена!</b>\n\n"
            f"📌 <b>Суть:</b> {html.escape(final_title)}"
            f"{desc_block}\n"
            f"📅 <b>Дедлайн:</b> {dl_block}\n\n"
            f"<i>Данные задачи актуализированы на основе новых сообщений.</i>"
        )

        in_cal = bool(existing_task.get("caldav_uid"))
        has_dl = bool(final_deadline)
        try:
            await bot.send_message(
                chat_id=MY_ID,
                text=notification_text,
                reply_markup=get_task_inline_keyboard(
                    target_task_id,
                    show_calendar_button=not in_cal,
                    is_in_calendar=in_cal,
                    has_deadline=has_dl,
                ),
            )
        except Exception as e:
            logger.error("Не удалось отправить уведомление об обновлении задачи: %s", e)


async def handle_trello_card_completed(card_id: str, card_title: str) -> None:
    """
    Вызывается TrelloSyncService при получении вебхука или обнаружении карточки
    в колонке Done/Завершено.
    Находит соответствующую активную задачу, помечает её выполненной, снимает
    напоминание из APScheduler и отправляет уведомление пользователю в Telegram.
    """
    task = await find_active_task_for_trello_card(card_id=card_id, card_title=card_title)
    if not task:
        logger.debug("Trello карточка %s ('%s') не привязана к активной задаче.", card_id, card_title)
        return

    task_id = task["id"]
    task_title = task["title"]
    logger.info("Задача #%d ('%s') завершена через Trello (card_id=%s)", task_id, task_title, card_id)

    # Обновляем статус задачи в БД
    await update_task_status(task_id, "completed")

    # Снимаем таймер из планировщика, если он был зарегистрирован
    job_id = f"task_reminder_{task_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    # Отправляем уведомление мужу (если bot доступен)
    if _current_bot:
        try:
            await _current_bot.send_message(
                chat_id=MY_ID,
                text=(
                    f"✅ <b>Задача #{task_id} выполнена в Trello!</b>\n\n"
                    f"📌 <b>Суть:</b> {html.escape(task_title)}\n\n"
                    f"<i>Карточка перемещена в колонку завершенных. Статус обновлен на 'completed'.</i>"
                ),
            )
        except Exception as e:
            logger.error(
                "Ошибка при отправке уведомления в Telegram о выполнении задачи #%d из Trello: %s",
                task_id,
                e,
            )


async def handle_trello_card_updated(
    card_id: str,
    card_title: str,
    card_desc: Optional[str] = None,
    card_due: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """
    Вызывается TrelloSyncService при изменении карточки в любой колонке Trello:
    синхронизирует обновленное название, описание и дедлайн обратно в базу данных SQLite.
    """
    target_path = _get_db_path(db_path)
    task = await find_active_task_for_trello_card(card_id=card_id, card_title=card_title, db_path=target_path)
    if not task:
        logger.debug("Trello карточка %s ('%s') не привязана к активной задаче для обновления.", card_id, card_title)
        return

    task_id = task["id"]
    current_title = task["title"]
    title_changed = bool(card_title and card_title.strip() != current_title.strip())

    deadline_str = task.get("deadline")
    deadline_changed = False
    deadline_dt: Optional[datetime] = None

    if card_due:
        try:
            dt = parse_deadline(card_due)
            if dt:
                new_dl_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                if new_dl_str != deadline_str:
                    deadline_str = new_dl_str
                    deadline_changed = True
                    deadline_dt = dt
        except Exception as e:
            logger.debug("Не удалось обработать due из Trello: %s", e)

    if title_changed or deadline_changed:
        new_title = card_title.strip() if title_changed else current_title
        await update_task(
            task_id=task_id,
            title=new_title,
            deadline=deadline_str,
            db_path=target_path,
        )
        logger.info(
            "Задача #%d успешно синхронизирована из Trello: '%s' -> '%s' (дедлайн: %s)",
            task_id,
            current_title,
            new_title,
            deadline_str,
        )

        # Если дедлайн обновился — перепланируем напоминание
        if deadline_changed and deadline_dt and _current_bot:
            await schedule_task_job(
                scheduler_instance=scheduler,
                bot=_current_bot,
                task_id=task_id,
                title=new_title,
                deadline_dt=deadline_dt,
                deadline_str=deadline_str,
            )


async def _debounced_process_messages(bot: Bot, user_id: int) -> None:
    """
    Корутина ожидания дебаунса: если новых сообщений нет в течение
    DEBOUNCE_DELAY_SECONDS, запускает анализ контекста под блокировкой.
    """
    try:
        await asyncio.sleep(DEBOUNCE_DELAY_SECONDS)
    except asyncio.CancelledError:
        return

    async with _processing_lock:
        await process_recent_context_with_ollama(bot=bot, user_id=user_id)


def reset_debounce_timer(user_id: int, bot: Bot) -> None:
    """Сбрасывает таймер дебаунса для указанного пользователя и регистрирует новую отложенную задачу."""
    if user_id in _debounce_tasks and not _debounce_tasks[user_id].done():
        _debounce_tasks[user_id].cancel()
        logger.debug("Сброшен предыдущий таймер дебаунса для user_id=%d.", user_id)

    _debounce_tasks[user_id] = asyncio.create_task(
        _debounced_process_messages(bot=bot, user_id=user_id)
    )


# ==============================================================================
# ОБРАБОТЧИКИ СОБЫТИЙ TELEGRAM (aiogram 3)
# ==============================================================================

router = Router(name="business_task_router")


@router.business_message(F.from_user.id == WIFE_ID)
async def handle_wife_business_message(message: types.Message, bot: Bot) -> None:
    """
    Перехватывает сообщения из Telegram Business чата с жестким фильтром по WIFE_ID.
    Поддерживает как текстовые, так и голосовые сообщения.
    """
    if message.from_user and message.from_user.id != WIFE_ID:
        logger.debug("Сообщение не от WIFE_ID, игнорируем.")
        return

    # 1. Голосовые сообщения
    if message.voice:
        logger.info("Получено голосовое сообщение от жены (ID %d).", WIFE_ID)
        await process_voice_message(
            message=message,
            bot=bot,
            save_chat_message_fn=save_chat_message,
            reset_debounce_fn=lambda uid: reset_debounce_timer(uid, bot),
            db_path=DB_PATH,
            ollama_url=OLLAMA_URL,
            ollama_model=OLLAMA_MODEL,
            ollama_timeout=OLLAMA_TIMEOUT_SECONDS,
        )
        return

    text = message.text or message.caption
    if not text:
        logger.debug("Получено сообщение от жены без текста (стикер/медиа без подписи), игнорируем.")
        return

    logger.info("Получено текстовое сообщение от жены (ID %d): '%s'", WIFE_ID, text)

    # 2. Сохраняем текстовое сообщение в историю чата для контекстного окна
    await save_chat_message(
        sender_id=WIFE_ID,
        text=text,
        sender_name="Жена",
        source_message_id=message.message_id,
        message_type="text",
    )

    # 3. Дебаунс: сбрасываем предыдущий таймер, если жена пишет серию сообщений подряд
    reset_debounce_timer(WIFE_ID, bot)


@router.edited_business_message(F.from_user.id == WIFE_ID)
async def handle_wife_edited_business_message(message: types.Message, bot: Bot) -> None:
    """
    Перехватывает отредактированные сообщения от жены в Telegram Business.
    Обновляет контекст и перезапускает дебаунс.
    """
    if message.from_user and message.from_user.id != WIFE_ID:
        return

    text = message.text or message.caption
    if not text:
        return

    logger.info("Жена отредактировала сообщение (ID %d): '%s'", WIFE_ID, text)

    # Сохраняем отредактированное сообщение в контекстную историю
    await save_chat_message(
        sender_id=WIFE_ID,
        text=f"(ред.) {text}",
        sender_name="Жена",
        source_message_id=message.message_id,
        message_type="text",
    )

    reset_debounce_timer(WIFE_ID, bot)


@router.business_message(F.from_user.id == MY_ID)
async def handle_husband_business_message(message: types.Message) -> None:
    """
    Перехватывает реплики мужа в Business-чате с женой.
    Сохраняет их в историю для полноты контекста, но НЕ запускает создание задач.
    """
    text = message.text or message.caption
    if not text:
        return

    logger.debug("Сохранение реплики мужа в контекстную историю: '%s'", text)
    await save_chat_message(
        sender_id=MY_ID,
        text=text,
        sender_name="Муж",
        source_message_id=message.message_id,
        message_type="text",
    )


@router.business_connection()
async def handle_business_connection(connection: types.BusinessConnection) -> None:
    """
    Служебный обработчик статуса подключения бота к Telegram Business.
    """
    logger.info(
        "Статус Telegram Business подключения (ID %s): user_id=%d, can_reply=%s, is_enabled=%s",
        connection.id,
        connection.user.id,
        connection.can_reply,
        connection.is_enabled,
    )


# ==============================================================================
# КОМАНДЫ ДЛЯ МУЖА (MY_ID) В ЛИЧНОМ ЧАТЕ С БОТОМ И ОБРАБОТКА INLINE-КНОПОК
# ==============================================================================


async def complete_task_by_id(
    task_id: int,
    task: Optional[Dict[str, Any]] = None,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Единая логика закрытия задачи:
    1. Помечает задачу как completed в SQLite базе данных.
    2. Удаляет таймер напоминания из APScheduler (если он ещё активен).
    3. Перемещает карточку задачи в колонку Done в Trello (если интеграция включена).
    """
    target_path = _get_db_path(db_path)
    if task is None:
        task = await get_task_by_id(task_id, db_path=target_path)

    await update_task_status(task_id, "completed", target_path)

    # Снимаем таймер из планировщика, если он ещё ждал дедлайна
    job_id = f"task_reminder_{task_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    # Если задача связана с карточкой Trello — перемещаем карточку в колонку Done
    if trello_service and trello_service.is_enabled and task and task.get("trello_card_id"):
        try:
            if not trello_service.done_list_ids and hasattr(trello_service, "resolve_board_lists"):
                await trello_service.resolve_board_lists()
            if trello_service.done_list_ids:
                done_list_id = next(iter(trello_service.done_list_ids))
                await trello_service.client.update_card(
                    card_id=task["trello_card_id"],
                    list_id=done_list_id,
                )
        except Exception as e:
            logger.error("Ошибка при перемещении карточки Trello в Done: %s", e)

    return task


@router.message(CommandStart(), F.from_user.id == MY_ID)
async def cmd_start(message: types.Message) -> None:
    """Информационное сообщение при старте бота."""
    asr_status = f"<b>включен</b> (<code>{ASR_MODEL_SIZE}</code>, <code>{ASR_DEVICE}</code>)" if ASR_ENABLED else "<b>выключен</b>"
    trello_status = "<b>включена</b>" if (trello_service and trello_service.is_enabled) else "<b>выключена</b>"
    cal_name_str = f" (<code>{calendar_service.active_calendar_name}</code>)" if (calendar_service and getattr(calendar_service, "active_calendar_name", None)) else ""
    caldav_status = f"<b>включен</b>{cal_name_str}" if (calendar_service and calendar_service.is_enabled) else "<b>выключен</b>"
    await message.answer(
        "👋 <b>Бот контекстного учета задач активен.</b>\n\n"
        "• Мониторинг Telegram Business: <b>включен</b>\n"
        f"• Отслеживаемый WIFE_ID: <code>{WIFE_ID}</code>\n"
        f"• Часовой пояс задач: <code>{TIMEZONE_STR}</code> (<code>{BOT_TIMEZONE}</code>)\n"
        f"• ИИ-модель задач: <code>{OLLAMA_MODEL}</code>\n"
        f"• Локальный ASR (Whisper): {asr_status}\n"
        f"• Режим резюме голосовых: <code>{VOICE_SUMMARY_MODE}</code>\n"
        f"• Apple Calendar (CalDAV): {caldav_status}\n"
        f"• Синхронизация с Trello: {trello_status}\n"
        f"• Контекстное окно: <b>{CONTEXT_HISTORY_LIMIT} сообщений</b>\n"
        f"• Задержка дебаунса: <b>{DEBOUNCE_DELAY_SECONDS} с</b>\n\n"
        "Команды:\n"
        "/tasks — список активных задач с inline-кнопками\n"
        "/done &lt;id&gt; — отметить задачу выполненной вручную"
    )


TASKS_PAGE_SIZE: int = 10


def render_tasks_page(
    tasks: List[Dict[str, Any]],
    page: int = 0,
    page_size: int = TASKS_PAGE_SIZE,
) -> Tuple[str, Optional[types.InlineKeyboardMarkup]]:
    """
    Формирует текст сообщения и клавиатуру для указанной страницы списка задач.
    Гарантирует, что размер сообщения не превысит лимит Telegram (4096 символов).
    """
    total_tasks = len(tasks)
    if total_tasks == 0:
        return "🎉 <b>Активных задач нет!</b> Все сделано.", None

    total_pages = max(1, (total_tasks + page_size - 1) // page_size)
    current_page = max(0, min(page, total_pages - 1))

    start_idx = current_page * page_size
    end_idx = min(start_idx + page_size, total_tasks)
    page_tasks = tasks[start_idx:end_idx]

    header = f"📋 <b>Текущие задачи (Стр. {current_page + 1}/{total_pages}, всего: {total_tasks}):</b>\n"
    lines = [header]
    keyboard_buttons: List[List[types.InlineKeyboardButton]] = []

    for t in page_tasks:
        category = t.get("category", "task")
        status_icon = "🗓" if category == "calendar" else ("⏳" if t["status"] == "pending" else "⏰")
        dl = f" (до <code>{t['deadline']}</code>)" if t.get("deadline") else " (без дедлайна)"
        item_text = f"{status_icon} <b>#{t['id']}</b>: {html.escape(t['title'])}{dl} [<code>{t['status']}</code>]"
        if t.get("location"):
            item_text += f"\n   📍 <i>{html.escape(t['location'])}</i>"
        if t.get("contact_info"):
            item_text += f"\n   📞 <i>{html.escape(t['contact_info'])}</i>"
        if t.get("meeting_url"):
            item_text += f"\n   🌐 <a href=\"{t['meeting_url']}\">Ссылка для созвона</a>"
        if t.get("trello_card_url"):
            item_text += f"\n   📋 <a href=\"{t['trello_card_url']}\">Карточка в Trello</a>"
        if t.get("description"):
            desc_preview = html.escape(t["description"])
            if len(desc_preview) > 100:
                desc_preview = desc_preview[:100] + "..."
            item_text += f"\n   🔗 <i>{desc_preview}</i>"
        lines.append(item_text)

        btn_title = t["title"][:20] + ("..." if len(t["title"]) > 20 else "")
        row = [
            types.InlineKeyboardButton(
                text=f"✅ #{t['id']} {btn_title}",
                callback_data=f"done:{t['id']}",
            )
        ]
        if t.get("caldav_uid"):
            row.append(
                types.InlineKeyboardButton(
                    text="🗓 В календаре",
                    callback_data="noop_cal",
                )
            )
        elif t.get("deadline"):
            row.append(
                types.InlineKeyboardButton(
                    text="📅 В календарь",
                    callback_data=f"to_calendar:{t['id']}",
                )
            )
        keyboard_buttons.append(row)

    # Навигационные кнопки пагинации (если страниц больше одной)
    if total_pages > 1:
        nav_row = []
        if current_page > 0:
            nav_row.append(
                types.InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"tasks_page:{current_page - 1}",
                )
            )
        nav_row.append(
            types.InlineKeyboardButton(
                text=f"📄 {current_page + 1}/{total_pages}",
                callback_data="noop_page",
            )
        )
        if current_page < total_pages - 1:
            nav_row.append(
                types.InlineKeyboardButton(
                    text="Вперед ➡️",
                    callback_data=f"tasks_page:{current_page + 1}",
                )
            )
        keyboard_buttons.append(nav_row)

    lines.append("\n<i>Нажмите на inline-кнопку под сообщением или отправьте: /done &lt;id&gt;</i>")
    message_text = "\n\n".join(lines)

    # Защита от превышения лимита 4096 символов Telegram
    if len(message_text) > 3900:
        message_text = message_text[:3850] + "\n\n... <i>(текст сокращен под лимит Telegram)</i>"

    reply_markup = types.InlineKeyboardMarkup(inline_keyboard=keyboard_buttons) if keyboard_buttons else None
    return message_text, reply_markup


@router.message(Command("tasks"), F.from_user.id == MY_ID)
async def cmd_tasks(message: types.Message) -> None:
    """
    Выводит список актуальных задач из базы данных с пагинацией по 10 штук на страницу
    и кнопками быстрого действия. Поддерживает вызов: /tasks или /tasks <номер_страницы>.
    """
    page = 0
    raw_text = message.text
    if isinstance(raw_text, str):
        args = raw_text.split()
        if len(args) > 1 and args[1].isdigit():
            page = max(0, int(args[1]) - 1)

    tasks = await get_active_tasks()
    text, reply_markup = render_tasks_page(tasks, page=page)
    await message.answer(text, reply_markup=reply_markup)


@router.message(Command("done"), F.from_user.id == MY_ID)
async def cmd_done(message: types.Message) -> None:
    """Позволяет отметить задачу как выполненную через команду: /done 12"""
    args = message.text.split() if message.text else []
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: <code>/done &lt;id_задачи&gt;</code> (например, <code>/done 1</code>)")
        return

    task_id = int(args[1])
    task = await get_task_by_id(task_id)
    if not task:
        await message.answer(f"Задача <b>#{task_id}</b> не найдена.")
        return

    if task.get("status") == "completed":
        await message.answer(f"Задача <b>#{task_id}</b> уже была отмечена как выполненная.")
        return

    await complete_task_by_id(task_id, task=task)
    await message.answer(f"✅ Задача <b>#{task_id}</b> отмечена как выполненная!")


@router.callback_query(F.data.startswith("done:"))
async def callback_done_task(callback: types.CallbackQuery) -> None:
    """Обработчик нажатия на inline кнопку быстрого завершения задачи."""
    data = callback.data or ""
    parts = data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await callback.answer("Некорректный ID задачи.", show_alert=True)
        return

    task_id = int(parts[1])
    task = await get_task_by_id(task_id)
    if not task:
        await callback.answer(f"Задача #{task_id} не найдена.", show_alert=True)
        return

    if task.get("status") == "completed":
        await callback.answer(f"Задача #{task_id} уже выполнена!", show_alert=False)
        try:
            if callback.message and callback.message.reply_markup:
                old_rows = callback.message.reply_markup.inline_keyboard
                if len(old_rows) <= 1:
                    done_markup = types.InlineKeyboardMarkup(
                        inline_keyboard=[[types.InlineKeyboardButton(text=f"✅ Задача #{task_id} завершена", callback_data="noop")]]
                    )
                    await callback.message.edit_reply_markup(reply_markup=done_markup)
                else:
                    new_rows = []
                    for row in old_rows:
                        new_row = []
                        for btn in row:
                            if btn.callback_data == f"done:{task_id}":
                                new_row.append(types.InlineKeyboardButton(text=f"✅ #{task_id} Завершена", callback_data="noop"))
                            elif btn.callback_data == f"to_calendar:{task_id}":
                                continue
                            else:
                                new_row.append(btn)
                        new_rows.append(new_row)
                    await callback.message.edit_reply_markup(reply_markup=types.InlineKeyboardMarkup(inline_keyboard=new_rows))
        except Exception:
            pass
        return

    await complete_task_by_id(task_id, task=task)
    await callback.answer(f"✅ Задача #{task_id} выполнена!", show_alert=False)

    try:
        if callback.message and callback.message.reply_markup:
            old_rows = callback.message.reply_markup.inline_keyboard
            if len(old_rows) <= 1:
                done_markup = types.InlineKeyboardMarkup(
                    inline_keyboard=[[types.InlineKeyboardButton(text=f"✅ Задача #{task_id} завершена", callback_data="noop")]]
                )
                await callback.message.edit_reply_markup(reply_markup=done_markup)
            else:
                new_rows = []
                for row in old_rows:
                    new_row = []
                    for btn in row:
                        if btn.callback_data == f"done:{task_id}":
                            new_row.append(types.InlineKeyboardButton(text=f"✅ #{task_id} Завершена", callback_data="noop"))
                        elif btn.callback_data == f"to_calendar:{task_id}":
                            continue
                        else:
                            new_row.append(btn)
                    new_rows.append(new_row)
                await callback.message.edit_reply_markup(reply_markup=types.InlineKeyboardMarkup(inline_keyboard=new_rows))
    except Exception as e:
        logger.debug("Не удалось обновить разметку inline-кнопки после завершения: %s", e)


@router.callback_query(F.data == "noop")
async def callback_noop(callback: types.CallbackQuery) -> None:
    """Заглушка для уже завершенных inline-кнопок."""
    await callback.answer("Эта задача уже отмечена как выполненная.", show_alert=False)


@router.callback_query(F.data.startswith("to_calendar:"))
async def callback_to_calendar(
    callback: types.CallbackQuery,
    db_path: Optional[str] = None,
) -> None:
    """
    Обработчик нажатия на inline кнопку «📅 В календарь».
    Позволяет перенести задачу в категорию 'calendar' и добавить её в Apple Calendar (CalDAV),
    если ИИ ошибся при автоматической классификации.
    """
    data = callback.data or ""
    parts = data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await callback.answer("Некорректный ID задачи.", show_alert=True)
        return

    task_id = int(parts[1])
    target_path = _get_db_path(db_path)
    task = await get_task_by_id(task_id, db_path=target_path)
    if not task:
        await callback.answer(f"Задача #{task_id} не найдена.", show_alert=True)
        return

    if task.get("status") == "completed":
        await callback.answer(f"Задача #{task_id} уже выполнена!", show_alert=True)
        return

    if task.get("caldav_uid"):
        await callback.answer("Это событие уже занесено в Apple Calendar.", show_alert=False)
        try:
            if callback.message and callback.message.reply_markup:
                old_rows = callback.message.reply_markup.inline_keyboard
                if len(old_rows) <= 1:
                    cal_markup = get_task_inline_keyboard(task_id, show_calendar_button=False, is_in_calendar=True)
                    await callback.message.edit_reply_markup(reply_markup=cal_markup)
                else:
                    new_rows = []
                    for row in old_rows:
                        new_row = []
                        for btn in row:
                            if btn.callback_data == f"to_calendar:{task_id}":
                                new_row.append(types.InlineKeyboardButton(text="🗓 В календаре", callback_data="noop_cal"))
                            else:
                                new_row.append(btn)
                        new_rows.append(new_row)
                    await callback.message.edit_reply_markup(reply_markup=types.InlineKeyboardMarkup(inline_keyboard=new_rows))
        except Exception:
            pass
        return

    # Определяем дату и время начала события
    start_dt = resolve_event_datetime(task)
    new_deadline_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")

    caldav_info = None
    if calendar_service and calendar_service.is_enabled:
        try:
            caldav_info = await calendar_service.create_event(
                title=task["title"],
                start_dt=start_dt,
                location=task.get("location"),
                description=task.get("description"),
                meeting_url=task.get("meeting_url"),
                contact_info=task.get("contact_info"),
            )
        except Exception as e:
            logger.error("Ошибка при создании события в CalDAV для задачи #%d: %s", task_id, e)
            await callback.answer("⚠️ Ошибка синхронизации с Apple Calendar.", show_alert=True)
            return

    caldav_uid = caldav_info["uid"] if caldav_info else None
    caldav_url = caldav_info.get("url", "") if caldav_info else None

    # Обновляем задачу в базе данных (категория -> calendar, caldav_uid, caldav_url)
    await update_task(
        task_id=task_id,
        category="calendar",
        deadline=new_deadline_str if not task.get("deadline") else None,
        caldav_uid=caldav_uid,
        caldav_url=caldav_url,
        db_path=target_path,
    )
    logger.info("Задача #%d переведена в категорию 'calendar' (CalDAV UID: %s)", task_id, caldav_uid)

    # Если дедлайн в будущем — планируем напоминание в APScheduler
    if _current_bot and start_dt > get_now():
        await schedule_task_job(
            scheduler_instance=scheduler,
            bot=_current_bot,
            task_id=task_id,
            title=task["title"],
            deadline_dt=start_dt,
            deadline_str=new_deadline_str,
        )

    if caldav_info:
        await callback.answer("📅 Добавлено в Apple Calendar!", show_alert=False)
    else:
        await callback.answer("🗓 Категория обновлена на 'Календарь'. CalDAV не настроен.", show_alert=True)

    try:
        if callback.message and callback.message.reply_markup:
            old_rows = callback.message.reply_markup.inline_keyboard
            if len(old_rows) <= 1:
                cal_markup = get_task_inline_keyboard(task_id, show_calendar_button=False, is_in_calendar=True)
                await callback.message.edit_reply_markup(reply_markup=cal_markup)
            else:
                new_rows = []
                for row in old_rows:
                    new_row = []
                    for btn in row:
                        if btn.callback_data == f"to_calendar:{task_id}":
                            new_row.append(types.InlineKeyboardButton(text="🗓 В календаре", callback_data="noop_cal"))
                        else:
                            new_row.append(btn)
                    new_rows.append(new_row)
                await callback.message.edit_reply_markup(reply_markup=types.InlineKeyboardMarkup(inline_keyboard=new_rows))
    except Exception as e:
        logger.debug("Не удалось обновить inline-кнопки после добавления в календарь: %s", e)


@router.callback_query(F.data.startswith("tasks_page:"))
async def callback_tasks_page(
    callback: types.CallbackQuery,
    db_path: Optional[str] = None,
) -> None:
    """Обработчик перелистывания страниц пагинации в /tasks."""
    data = callback.data or ""
    parts = data.split(":")
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

    target_path = _get_db_path(db_path)
    tasks = await get_active_tasks(db_path=target_path)
    text, reply_markup = render_tasks_page(tasks, page=page)
    try:
        if callback.message:
            await callback.message.edit_text(text, reply_markup=reply_markup)
    except Exception as e:
        logger.debug("Не удалось обновить страницу /tasks: %s", e)
    await callback.answer()


@router.callback_query(F.data == "noop_page")
async def callback_noop_page(callback: types.CallbackQuery) -> None:
    """Заглушка для индикатора текущей страницы."""
    await callback.answer()


@router.callback_query(F.data == "noop_cal")
async def callback_noop_cal(callback: types.CallbackQuery) -> None:
    """Заглушка для уже добавленных в календарь событий."""
    await callback.answer("Это событие уже занесено в календарь.", show_alert=False)


# ==============================================================================
# ТОЧКА ВХОДА И ЖИЗНЕННЫЙ ЦИКЛ ПРИЛОЖЕНИЯ
# ==============================================================================


async def on_startup(bot: Bot) -> None:
    """Действия при старте бота."""
    global _current_bot
    _current_bot = bot
    logger.info("Запуск бота...")
    await init_db(DB_PATH)

    if not scheduler.running:
        scheduler.start()
        logger.info("Планировщик APScheduler запущен (часовой пояс: %s).", BOT_TIMEZONE)

    await restore_scheduled_jobs(bot, scheduler, DB_PATH)

    # Инициализация и запуск сервиса синхронизации Trello
    if trello_service and trello_service.is_enabled:
        await trello_service.start(
            scheduler=scheduler,
            on_card_completed=handle_trello_card_completed,
            on_card_updated=handle_trello_card_updated,
        )

    # Проверка подключения к Apple Calendar (CalDAV) при старте бота, если включен в конфиге
    if calendar_service and calendar_service.enabled:
        logger.info("Проверка доступности Apple Calendar (CalDAV)...")
        cal_name = await calendar_service.check_connection()
        logger.info("✅ Связь с календарем Apple (CalDAV) есть! Активный календарь: '%s'", cal_name)

    logger.info("Бот готов к обработке событий Telegram Business.")


async def on_shutdown() -> None:
    """Корректное завершение работы бота."""
    logger.info("Остановка бота...")
    if trello_service and trello_service.is_enabled:
        await trello_service.stop()
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Планировщик APScheduler остановлен.")
    await close_pg_pool()


async def main() -> None:
    """Главная функция инициализации и запуска polling."""
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logger.warning(
            "ВНИМАНИЕ: BOT_TOKEN не задан! Укажите его в коде или в файле .env."
        )

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp.include_router(router)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Работа бота завершена пользователем.")
