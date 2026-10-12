import asyncio
from datetime import datetime
import html
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

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
OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
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

# ==============================================================================
# РАБОТА С БАЗОЙ ДАННЫХ SQLITE (aiosqlite)
# ==============================================================================


async def init_db(db_path: str = DB_PATH) -> None:
    """Инициализирует таблицы tasks и chat_history в базе данных SQLite."""
    async with aiosqlite.connect(db_path) as db:
        # Таблица задач
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                deadline TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
            """
        )

        # Миграция: проверяем наличие новых колонок на случай уже существующей БД
        cursor = await db.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in await cursor.fetchall()]
        if "description" not in columns:
            await db.execute("ALTER TABLE tasks ADD COLUMN description TEXT")
        if "updated_at" not in columns:
            await db.execute("ALTER TABLE tasks ADD COLUMN updated_at TEXT")

        # Таблица истории сообщений для контекстного анализа
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                sender_name TEXT,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.commit()
    logger.info("База данных SQLite инициализирована (таблицы 'tasks' и 'chat_history' готовы).")


async def save_chat_message(
    sender_id: int,
    text: str,
    sender_name: str = "Жена",
    db_path: str = DB_PATH,
) -> None:
    """Сохраняет сообщение в таблицу chat_history для последующего контекстного анализа."""
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO chat_history (sender_id, sender_name, text, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (sender_id, sender_name, text, created_at),
        )
        await db.commit()


async def get_recent_chat_history(
    limit: int = CONTEXT_HISTORY_LIMIT,
    db_path: str = DB_PATH,
) -> List[Dict[str, Any]]:
    """Получает последние N сообщений в хронологическом порядке (от старых к новым)."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, sender_id, sender_name, text, created_at
            FROM chat_history
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in reversed(rows)]


async def get_latest_active_task(db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    """Получает последнюю активную задачу со статусом pending или reminded."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, title, description, deadline, status, created_at, updated_at
            FROM tasks
            WHERE status IN ('pending', 'reminded')
            ORDER BY id DESC
            LIMIT 1
            """
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_task_by_id(task_id: int, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    """Возвращает задачу по её первичному ключу ID."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, title, description, deadline, status, created_at, updated_at
            FROM tasks
            WHERE id = ?
            """,
            (task_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def create_task(
    title: str,
    description: Optional[str] = None,
    deadline: Optional[str] = None,
    db_path: str = DB_PATH,
) -> int:
    """Создает новую задачу в базе данных и возвращает её ID."""
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            """
            INSERT INTO tasks (title, description, deadline, status, created_at, updated_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (title, description, deadline, created_at, created_at),
        )
        await db.commit()
        return cursor.lastrowid


async def update_task(
    task_id: int,
    title: Optional[str] = None,
    description: Optional[str] = None,
    deadline: Optional[str] = None,
    status: Optional[str] = None,
    db_path: str = DB_PATH,
) -> bool:
    """Обновляет поля существующей задачи."""
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fields = ["updated_at = ?"]
    values: List[Any] = [updated_at]

    if title is not None:
        fields.append("title = ?")
        values.append(title)
    if description is not None:
        fields.append("description = ?")
        values.append(description)
    if deadline is not None:
        fields.append("deadline = ?")
        values.append(deadline)
    if status is not None:
        fields.append("status = ?")
        values.append(status)

    values.append(task_id)
    query = f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?"

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(query, values)
        await db.commit()
        return cursor.rowcount > 0


async def update_task_status(
    task_id: int,
    status: str,
    db_path: str = DB_PATH,
) -> None:
    """Обновляет только статус задачи (pending / reminded / completed)."""
    await update_task(task_id=task_id, status=status, db_path=db_path)
    logger.info("Статус задачи #%d обновлен на '%s'.", task_id, status)


async def get_pending_tasks_with_deadline(
    db_path: str = DB_PATH,
) -> List[Dict[str, Any]]:
    """Получает все незавершенные задачи с установленным дедлайном для APScheduler."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, title, description, deadline, status, created_at
            FROM tasks
            WHERE status = 'pending' AND deadline IS NOT NULL
            """
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_active_tasks(db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Получает список всех актуальных задач для вывода пользователю."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, title, description, deadline, status, created_at
            FROM tasks
            WHERE status IN ('pending', 'reminded')
            ORDER BY id DESC
            """
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


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
1. ДЕЙСТВИЕ "create" (создать новую задачу):
   - Выбирай, если в последних сообщениях появилось новое поручение, просьба что-то сделать, купить, забрать, выбрать, оплатить или встретиться.
   - СВЯЗКА КОНТЕКСТА (Ссылки ДО задачи): Если жена сначала прислала ссылки/фото/товары, а затем написала просьбу (например: «выбери игрушку для сына»), ты ОБЯЗАН объединить это:
     * "task_title": краткая и ясная суть задачи (например: "Выбрать игрушку для сына на день рождения")
     * "task_description": собери все ссылки, наименования и детали из предыдущих сообщений в связный список.

2. ДЕЙСТВИЕ "update" (дополнить/отредактировать существующую задачу):
   - Выбирай, если задача УЖЕ была создана ранее (см. "ПОСЛЕДНЯЯ СОЗДАННАЯ АКТИВНАЯ ЗАДАЧА"), а в самых свежих сообщениях жена присылает дополнительные ссылки, уточнения, изменения сроков или детали к ней.
   - Укажи "task_id" этой задачи, а в "task_description" приведи новые/дополненные детали и ссылки.

3. ДЕЙСТВИЕ "none" (задача не требуется):
   - Обычный разговор, эмоции, благодарность ("спасибо!", "люблю"), вопрос без поручения ("ты где?"), мем или приветствие.

ТРЕБОВАНИЯ К ФОРМАТУ ОТВЕТА:
Строго верни валидный JSON без markdown-разметки и вводных слов по схеме:
{{
  "action": "create" | "update" | "none",
  "is_task": true/false,
  "task_id": 123 (число ID задачи при action="update", иначе null),
  "task_title": "Суть задачи коротко и ясно" (или null),
  "task_description": "Ссылки, варианты и пояснения" (или null),
  "has_deadline": true/false,
  "deadline_timestamp": "YYYY-MM-DD HH:MM:SS" (или null)
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
    current_dt = datetime.now()
    prompt = build_prompt(
        recent_messages=recent_messages,
        latest_task=latest_task,
        current_dt=current_dt,
    )

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
    }

    logger.info("Отправка контекста (%d сообщений) в Ollama (%s)...", len(recent_messages), model)

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


def parse_deadline(deadline_str: Optional[str]) -> Optional[datetime]:
    """
    Парсит строку даты/времени в объект datetime с поддержкой нескольких форматов.
    """
    if not deadline_str or not isinstance(deadline_str, str):
        return None

    deadline_str = deadline_str.strip()
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
            return dt
        except ValueError:
            continue

    logger.warning("Не удалось распарсить дедлайн '%s'.", deadline_str)
    return None


# ==============================================================================
# ПЛАНИРОВЩИК НАПОМИНАНИЙ (APScheduler)
# ==============================================================================

scheduler = AsyncIOScheduler()


async def send_task_reminder(
    bot: Bot,
    task_id: int,
    title: str,
    deadline_str: str,
    my_id: int = MY_ID,
    db_path: str = DB_PATH,
) -> None:
    """
    Callback-функция для APScheduler: отправляет уведомление пользователю MY_ID,
    когда наступает дедлайн задачи, и обновляет статус задачи в базе данных.
    """
    logger.info("Срабатывание напоминания по дедлайну для задачи #%d ('%s')", task_id, title)
    task = await get_task_by_id(task_id, db_path=db_path)
    description = task.get("description") if task else None

    desc_block = f"\n🔗 <b>Детали:</b>\n{html.escape(description)}\n" if description else ""

    message_text = (
        f"⏰ <b>НАПОМИНАНИЕ О ДЕДЛАЙНЕ!</b>\n\n"
        f"📌 <b>Задача:</b> {html.escape(title)}"
        f"{desc_block}\n"
        f"⏳ <b>Дедлайн:</b> <code>{deadline_str}</code>\n\n"
        f"<i>Задача #{task_id} ожидает выполнения. Для закрытия: /done {task_id}</i>"
    )

    try:
        await bot.send_message(chat_id=my_id, text=message_text)
        await update_task_status(task_id, "reminded", db_path)
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
    now = datetime.now()
    if deadline_dt <= now:
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
        trigger=DateTrigger(run_date=deadline_dt),
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
    db_path: str = DB_PATH,
) -> None:
    """
    Восстанавливает активные напоминания из SQLite при перезапуске бота.
    """
    pending_tasks = await get_pending_tasks_with_deadline(db_path)
    restored_count = 0
    now = datetime.now()

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

        task_id = await create_task(
            title=task_title,
            description=task_desc,
            deadline=deadline_str,
        )
        logger.info("Создана новая задача #%d: '%s' (Дедлайн: %s)", task_id, task_title, deadline_str)

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

        notification_text = (
            f"📥 <b>Новая задача от жены!</b>\n\n"
            f"📌 <b>Суть:</b> {html.escape(task_title)}"
            f"{desc_block}\n"
            f"📅 <b>Дедлайн:</b> {deadline_info}\n\n"
            f"<i>Запись сохранена в базу (ID: #{task_id})</i>"
        )

        try:
            await bot.send_message(chat_id=MY_ID, text=notification_text)
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

        await update_task(
            task_id=target_task_id,
            title=final_title,
            description=final_desc,
            deadline=final_deadline,
        )
        logger.info("Задача #%d успешно обновлена новыми данными.", target_task_id)

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

        try:
            await bot.send_message(chat_id=MY_ID, text=notification_text)
        except Exception as e:
            logger.error("Не удалось отправить уведомление об обновлении задачи: %s", e)


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


# ==============================================================================
# ОБРАБОТЧИКИ СОБЫТИЙ TELEGRAM (aiogram 3)
# ==============================================================================

router = Router(name="business_task_router")


@router.business_message(F.from_user.id == WIFE_ID)
async def handle_wife_business_message(message: types.Message, bot: Bot) -> None:
    """
    Перехватывает сообщения из Telegram Business чата с жестким фильтром по WIFE_ID.
    Сохраняет сообщения в контекстную историю и запускает таймер дебаунса.
    """
    text = message.text or message.caption
    if not text:
        logger.debug("Получено сообщение от жены без текста (стикер/медиа без подписи), игнорируем.")
        return

    logger.info("Получено сообщение от жены (ID %d): '%s'", WIFE_ID, text)

    # 1. Сохраняем сообщение в историю чата для контекстного окна
    await save_chat_message(
        sender_id=WIFE_ID,
        text=text,
        sender_name="Жена",
    )

    # 2. Дебаунс: сбрасываем предыдущий таймер, если жена пишет серию сообщений подряд
    if WIFE_ID in _debounce_tasks and not _debounce_tasks[WIFE_ID].done():
        _debounce_tasks[WIFE_ID].cancel()
        logger.debug("Сброшен предыдущий таймер дебаунса для WIFE_ID.")

    # 3. Регистрируем новую отложенную задачу обработки
    _debounce_tasks[WIFE_ID] = asyncio.create_task(
        _debounced_process_messages(bot=bot, user_id=WIFE_ID)
    )


# ==============================================================================
# КОМАНДЫ ДЛЯ МУЖА (MY_ID) В ЛИЧНОМ ЧАТЕ С БОТОМ
# ==============================================================================


@router.message(CommandStart(), F.from_user.id == MY_ID)
async def cmd_start(message: types.Message) -> None:
    """Информационное сообщение при старте бота."""
    await message.answer(
        "👋 <b>Бот контекстного учета задач активен.</b>\n\n"
        "• Мониторинг Telegram Business: <b>включен</b>\n"
        f"• Отслеживаемый WIFE_ID: <code>{WIFE_ID}</code>\n"
        f"• ИИ-модель: <code>{OLLAMA_MODEL}</code>\n"
        f"• Контекстное окно: <b>{CONTEXT_HISTORY_LIMIT} сообщений</b>\n"
        f"• Задержка дебаунса: <b>{DEBOUNCE_DELAY_SECONDS} с</b>\n\n"
        "Команды:\n"
        "/tasks — список активных задач с деталями\n"
        "/done &lt;id&gt; — отметить задачу выполненной"
    )


@router.message(Command("tasks"), F.from_user.id == MY_ID)
async def cmd_tasks(message: types.Message) -> None:
    """Выводит список актуальных задач из базы данных."""
    tasks = await get_active_tasks()
    if not tasks:
        await message.answer("🎉 <b>Активных задач нет!</b> Все сделано.")
        return

    lines = ["📋 <b>Текущие задачи:</b>\n"]
    for t in tasks:
        status_icon = "⏳" if t["status"] == "pending" else "⏰"
        dl = f" (до <code>{t['deadline']}</code>)" if t["deadline"] else " (без дедлайна)"
        item_text = f"{status_icon} <b>#{t['id']}</b>: {html.escape(t['title'])}{dl} [<code>{t['status']}</code>]"
        if t.get("description"):
            # Краткий предпросмотр деталей
            desc_preview = html.escape(t["description"])
            if len(desc_preview) > 200:
                desc_preview = desc_preview[:200] + "..."
            item_text += f"\n   🔗 <i>{desc_preview}</i>"
        lines.append(item_text)

    lines.append("\n<i>Чтобы закрыть задачу: /done &lt;id&gt;</i>")
    await message.answer("\n\n".join(lines))


@router.message(Command("done"), F.from_user.id == MY_ID)
async def cmd_done(message: types.Message) -> None:
    """Позволяет отметить задачу как выполненную: /done 12"""
    args = message.text.split() if message.text else []
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: <code>/done &lt;id_задачи&gt;</code> (например, <code>/done 1</code>)")
        return

    task_id = int(args[1])
    await update_task_status(task_id, "completed")

    # Снимаем таймер из планировщика, если он ещё ждал дедлайна
    job_id = f"task_reminder_{task_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    await message.answer(f"✅ Задача <b>#{task_id}</b> отмечена как выполненная!")


# ==============================================================================
# ТОЧКА ВХОДА И ЖИЗНЕННЫЙ ЦИКЛ ПРИЛОЖЕНИЯ
# ==============================================================================


async def on_startup(bot: Bot) -> None:
    """Действия при старте бота."""
    logger.info("Запуск бота...")
    await init_db(DB_PATH)

    scheduler.start()
    logger.info("Планировщик APScheduler запущен.")

    await restore_scheduled_jobs(bot, scheduler, DB_PATH)
    logger.info("Бот готов к обработке событий Telegram Business.")


async def on_shutdown() -> None:
    """Корректное завершение работы бота."""
    logger.info("Остановка бота...")
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Планировщик APScheduler остановлен.")


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
