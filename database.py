import asyncio
from datetime import datetime
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    import asyncpg
except ImportError:
    asyncpg = None

import aiosqlite
from tz_utils import get_now

logger = logging.getLogger("FamilyTasksBot.Database")

# ==============================================================================
# КОНФИГУРАЦИЯ БАЗЫ ДАННЫХ
# ==============================================================================

# Тип БД: "postgres" или "sqlite" (по умолчанию postgres, если задан POSTGRES_HOST или DATABASE_URL)
DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL")
POSTGRES_HOST: str = os.getenv("POSTGRES_HOST", "shared-postgres" if os.path.exists("/.dockerenv") else "localhost")
POSTGRES_PORT: int = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_USER: str = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD: str = os.getenv("POSTGRES_PASSWORD", "postgres")
POSTGRES_DB: str = os.getenv("POSTGRES_DB", "family_tasks_bot")

DB_PATH: str = os.getenv("DB_PATH", "tasks.db")

# Автоматическое определение типа БД:
_raw_db_type = os.getenv("DB_TYPE", "").strip().lower()
if _raw_db_type in ("postgres", "postgresql", "pg"):
    DEFAULT_ENGINE = "postgres"
elif _raw_db_type in ("sqlite", "sqlite3"):
    DEFAULT_ENGINE = "sqlite"
elif DATABASE_URL or os.getenv("POSTGRES_HOST"):
    DEFAULT_ENGINE = "postgres"
else:
    DEFAULT_ENGINE = "postgres"

# Глобальный пул соединений asyncpg
_pg_pool: Optional[Any] = None
_pool_lock = asyncio.Lock()


def _get_db_path(db_path: Optional[str] = None) -> str:
    """Возвращает переданный путь к БД или актуальное значение DB_PATH."""
    if db_path is not None:
        return db_path
    try:
        import bot
        return getattr(bot, "DB_PATH", DB_PATH)
    except Exception:
        return DB_PATH


def is_postgres_configured(db_path: Optional[str] = None) -> bool:
    """
    Определяет, должен ли запрос выполняться в PostgreSQL.
    Если передан путь к файлу SQLite (например, в юнит-тестах tmp_db), возвращает False.
    """
    if os.getenv("DB_TYPE", "").lower() in ("sqlite", "sqlite3"):
        return False

    target = _get_db_path(db_path)
    if target and target != "tasks.db" and (target.endswith(".db") or target.endswith(".sqlite") or "tmp" in target):
        return False

    if db_path is not None and db_path != "tasks.db" and (db_path.endswith(".db") or db_path.endswith(".sqlite")):
        return False

    if os.getenv("DB_TYPE", "").lower() in ("postgres", "postgresql", "pg"):
        return True
    if os.getenv("POSTGRES_HOST") or os.getenv("DATABASE_URL") or os.path.exists("/.dockerenv"):
        return True
    return DEFAULT_ENGINE == "postgres"


async def ensure_postgres_db_exists() -> None:
    """
    Проверяет существование целевой базы данных на сервере PostgreSQL.
    Если базы нет — автоматически создает ее (подключаясь к служебной базе 'postgres').
    Это позволяет использовать один PostgreSQL контейнер для разных проектов,
    просто меняя POSTGRES_DB в .env.
    """
    if asyncpg is None:
        raise RuntimeError("Пакет 'asyncpg' не установлен. Установите: pip install asyncpg")

    target_db = POSTGRES_DB
    admin_db = "postgres"

    try:
        if DATABASE_URL:
            # Если задан DATABASE_URL, пробуем подключиться напрямую или извлечь admin_url
            conn = await asyncpg.connect(DATABASE_URL)
            await conn.close()
            return
    except asyncpg.InvalidCatalogNameError:
        # База не существует, переключаемся на admin db
        logger.info("База данных '%s' не найдена. Создание через административную базу...", target_db)
    except Exception as e:
        logger.debug("Проверка DATABASE_URL завершилась с ошибкой (%s), проверяем по параметрам...", e)

    try:
        admin_conn = await asyncpg.connect(
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            database=admin_db,
        )
        try:
            exists = await admin_conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", target_db
            )
            if not exists:
                logger.info("📦 База данных '%s' отсутствует на сервере. Создаем...", target_db)
                # CREATE DATABASE не может выполняться внутри транзакции
                await admin_conn.execute(f'CREATE DATABASE "{target_db}"')
                logger.info("✅ База данных '%s' успешно создана на общем PostgreSQL!", target_db)
            else:
                logger.debug("База данных '%s' уже существует.", target_db)
        finally:
            await admin_conn.close()
    except Exception as e:
        logger.warning("Не удалось выполнить автоматическую проверку/создание БД '%s' через admin_conn: %s", target_db, e)


async def get_pg_pool() -> Any:
    """Возвращает или создает глобальный пул соединений asyncpg."""
    global _pg_pool
    if _pg_pool is not None and not _pg_pool.is_closing():
        return _pg_pool

    async with _pool_lock:
        if _pg_pool is not None and not _pg_pool.is_closing():
            return _pg_pool

        if asyncpg is None:
            raise RuntimeError("Пакет 'asyncpg' не установлен!")

        await ensure_postgres_db_exists()

        if DATABASE_URL:
            _pg_pool = await asyncpg.create_pool(
                dsn=DATABASE_URL,
                min_size=2,
                max_size=10,
                command_timeout=60,
            )
        else:
            _pg_pool = await asyncpg.create_pool(
                host=POSTGRES_HOST,
                port=POSTGRES_PORT,
                user=POSTGRES_USER,
                password=POSTGRES_PASSWORD,
                database=POSTGRES_DB,
                min_size=2,
                max_size=10,
                command_timeout=60,
            )
        logger.info(
            "✅ Пул соединений PostgreSQL инициализирован (хост: %s:%s, БД: %s, пользователь: %s).",
            POSTGRES_HOST,
            POSTGRES_PORT,
            POSTGRES_DB,
            POSTGRES_USER,
        )
        return _pg_pool


async def close_pg_pool() -> None:
    """Закрывает пул соединений PostgreSQL."""
    global _pg_pool
    if _pg_pool is not None:
        await _pg_pool.close()
        _pg_pool = None
        logger.info("Пул соединений PostgreSQL закрыт.")


# ==============================================================================
# ИНИЦИАЛИЗАЦИЯ ТАБЛИЦ И МИГРАЦИИ
# ==============================================================================


async def init_postgres_schema(pool: Any) -> None:
    """Создает таблицы и индексы в PostgreSQL."""
    async with pool.acquire() as conn:
        # Таблица tasks
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                deadline TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT,
                trello_card_id TEXT,
                trello_card_url TEXT,
                category TEXT DEFAULT 'task',
                location TEXT,
                meeting_url TEXT,
                contact_info TEXT,
                caldav_uid TEXT,
                caldav_url TEXT
            );
            """
        )

        # Таблица chat_history
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                id SERIAL PRIMARY KEY,
                sender_id BIGINT NOT NULL,
                sender_name TEXT,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                source_message_id BIGINT,
                message_type TEXT DEFAULT 'text'
            );
            """
        )

        # Таблица voice_messages
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_messages (
                id SERIAL PRIMARY KEY,
                telegram_message_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                business_connection_id TEXT,
                sender_id BIGINT NOT NULL,
                telegram_file_id TEXT,
                audio_path TEXT,
                transcript TEXT,
                summary TEXT,
                category TEXT,
                processing_status TEXT NOT NULL DEFAULT 'pending',
                error_message TEXT,
                created_at TEXT NOT NULL,
                processed_at TEXT,
                CONSTRAINT uq_voice_chat_msg UNIQUE(chat_id, telegram_message_id)
            );
            """
        )

        # Индексы
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_voice_messages_chat_msg
            ON voice_messages(chat_id, telegram_message_id);

            CREATE INDEX IF NOT EXISTS idx_tasks_status
            ON tasks(status);

            CREATE INDEX IF NOT EXISTS idx_tasks_trello_card_id
            ON tasks(trello_card_id);

            CREATE INDEX IF NOT EXISTS idx_chat_history_id
            ON chat_history(id);
            """
        )

        # Миграция: добавляем колонки в tasks если отсутствуют
        for col_name, col_type in [
            ("description", "TEXT"),
            ("updated_at", "TEXT"),
            ("trello_card_id", "TEXT"),
            ("trello_card_url", "TEXT"),
            ("category", "TEXT DEFAULT 'task'"),
            ("location", "TEXT"),
            ("meeting_url", "TEXT"),
            ("contact_info", "TEXT"),
            ("caldav_uid", "TEXT"),
            ("caldav_url", "TEXT"),
        ]:
            await conn.execute(f"ALTER TABLE tasks ADD COLUMN IF NOT EXISTS {col_name} {col_type};")

        # Миграция chat_history
        await conn.execute("ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS source_message_id BIGINT;")
        await conn.execute("ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS message_type TEXT DEFAULT 'text';")


async def init_sqlite_schema(target_path: str) -> None:
    """Инициализирует схему SQLite (для тестов или локального режима без PG)."""
    async with aiosqlite.connect(target_path) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                deadline TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT,
                trello_card_id TEXT,
                trello_card_url TEXT,
                category TEXT DEFAULT 'task',
                location TEXT,
                meeting_url TEXT,
                contact_info TEXT,
                caldav_uid TEXT,
                caldav_url TEXT
            )
            """
        )
        cursor = await db.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in await cursor.fetchall()]
        for col in [
            "description", "updated_at", "trello_card_id", "trello_card_url",
            "category", "location", "meeting_url", "contact_info", "caldav_uid", "caldav_url"
        ]:
            if col not in columns:
                extra = " DEFAULT 'task'" if col == "category" else ""
                await db.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT{extra}")

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
        cursor = await db.execute("PRAGMA table_info(chat_history)")
        chat_columns = [row[1] for row in await cursor.fetchall()]
        if "source_message_id" not in chat_columns:
            await db.execute("ALTER TABLE chat_history ADD COLUMN source_message_id INTEGER")
        if "message_type" not in chat_columns:
            await db.execute("ALTER TABLE chat_history ADD COLUMN message_type TEXT DEFAULT 'text'")

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_message_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                business_connection_id TEXT,
                sender_id INTEGER NOT NULL,
                telegram_file_id TEXT,
                audio_path TEXT,
                transcript TEXT,
                summary TEXT,
                category TEXT,
                processing_status TEXT NOT NULL DEFAULT 'pending',
                error_message TEXT,
                created_at TEXT NOT NULL,
                processed_at TEXT,
                UNIQUE(chat_id, telegram_message_id)
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_voice_messages_chat_msg
            ON voice_messages(chat_id, telegram_message_id)
            """
        )
        await db.commit()


async def migrate_sqlite_to_postgres(sqlite_path: str = "tasks.db", pool: Optional[Any] = None) -> Dict[str, int]:
    """
    Переносит все существующие данные из SQLite (tasks.db) в PostgreSQL.
    Безопасно для повторного запуска (использует ON CONFLICT DO NOTHING).
    Синхронизирует счетчики первичных ключей (SERIAL sequences).
    """
    if not os.path.exists(sqlite_path):
        logger.info("Файл SQLite '%s' не найден, миграция данных пропущена.", sqlite_path)
        return {"tasks": 0, "chat_history": 0, "voice_messages": 0}

    if pool is None:
        pool = await get_pg_pool()

    counts = {"tasks": 0, "chat_history": 0, "voice_messages": 0}

    import sqlite3
    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cur = sqlite_conn.cursor()

    async with pool.acquire() as pg_conn:
        # 1. Миграция tasks
        try:
            sqlite_cur.execute(
                """
                SELECT id, title, description, deadline, status, created_at, updated_at,
                       trello_card_id, trello_card_url, category, location, meeting_url,
                       contact_info, caldav_uid, caldav_url
                FROM tasks
                ORDER BY id ASC
                """
            )
            rows = sqlite_cur.fetchall()
            for r in rows:
                await pg_conn.execute(
                    """
                    INSERT INTO tasks (
                        id, title, description, deadline, status, created_at, updated_at,
                        trello_card_id, trello_card_url, category, location, meeting_url,
                        contact_info, caldav_uid, caldav_url
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    r["id"], r["title"], r["description"], r["deadline"], r["status"],
                    r["created_at"], r["updated_at"], r["trello_card_id"], r["trello_card_url"],
                    r["category"], r["location"], r["meeting_url"], r["contact_info"],
                    r["caldav_uid"], r["caldav_url"],
                )
            counts["tasks"] = len(rows)

            # Обновление sequences
            await pg_conn.execute(
                "SELECT setval('tasks_id_seq', COALESCE((SELECT MAX(id) FROM tasks), 1));"
            )
        except Exception as e:
            logger.error("Ошибка при переносе таблицы tasks из SQLite: %s", e)

        # 2. Миграция chat_history
        try:
            sqlite_cur.execute(
                """
                SELECT id, sender_id, sender_name, text, created_at,
                       source_message_id, message_type
                FROM chat_history
                ORDER BY id ASC
                """
            )
            rows = sqlite_cur.fetchall()
            for r in rows:
                await pg_conn.execute(
                    """
                    INSERT INTO chat_history (
                        id, sender_id, sender_name, text, created_at,
                        source_message_id, message_type
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    r["id"], r["sender_id"], r["sender_name"], r["text"], r["created_at"],
                    r["source_message_id"], r["message_type"] or "text",
                )
            counts["chat_history"] = len(rows)

            await pg_conn.execute(
                "SELECT setval('chat_history_id_seq', COALESCE((SELECT MAX(id) FROM chat_history), 1));"
            )
        except Exception as e:
            logger.error("Ошибка при переносе таблицы chat_history из SQLite: %s", e)

        # 3. Миграция voice_messages
        try:
            sqlite_cur.execute(
                """
                SELECT id, telegram_message_id, chat_id, business_connection_id,
                       sender_id, telegram_file_id, audio_path, transcript, summary,
                       category, processing_status, error_message, created_at, processed_at
                FROM voice_messages
                ORDER BY id ASC
                """
            )
            rows = sqlite_cur.fetchall()
            for r in rows:
                await pg_conn.execute(
                    """
                    INSERT INTO voice_messages (
                        id, telegram_message_id, chat_id, business_connection_id,
                        sender_id, telegram_file_id, audio_path, transcript, summary,
                        category, processing_status, error_message, created_at, processed_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                    ON CONFLICT (chat_id, telegram_message_id) DO NOTHING
                    """,
                    r["id"], r["telegram_message_id"], r["chat_id"], r["business_connection_id"],
                    r["sender_id"], r["telegram_file_id"], r["audio_path"], r["transcript"],
                    r["summary"], r["category"], r["processing_status"], r["error_message"],
                    r["created_at"], r["processed_at"],
                )
            counts["voice_messages"] = len(rows)

            await pg_conn.execute(
                "SELECT setval('voice_messages_id_seq', COALESCE((SELECT MAX(id) FROM voice_messages), 1));"
            )
        except Exception as e:
            logger.error("Ошибка при переносе таблицы voice_messages из SQLite: %s", e)

    sqlite_conn.close()
    logger.info(
        "🎉 Миграция SQLite -> PostgreSQL успешно выполнена! Перенесено: %d задач, %d записей истории, %d голосовых сообщений.",
        counts["tasks"],
        counts["chat_history"],
        counts["voice_messages"],
    )
    return counts


async def init_db(db_path: Optional[str] = None) -> None:
    """Инициализирует базу данных (PostgreSQL или SQLite)."""
    if is_postgres_configured(db_path):
        pool = await get_pg_pool()
        await init_postgres_schema(pool)

        # Если таблица tasks пустая, а tasks.db существует — делаем авто-миграцию!
        async with pool.acquire() as conn:
            task_count = await conn.fetchval("SELECT COUNT(*) FROM tasks")
            if task_count == 0 and os.path.exists(DB_PATH):
                logger.info("Обнаружена существующая база SQLite '%s' при пустом PostgreSQL. Запуск миграции...", DB_PATH)
                await migrate_sqlite_to_postgres(DB_PATH, pool)

        logger.info("База данных PostgreSQL инициализирована и готова к работе.")
    else:
        target_path = _get_db_path(db_path)
        await init_sqlite_schema(target_path)
        logger.info("База данных SQLite инициализирована (путь: %s).", target_path)


# ==============================================================================
# ОПЕРАЦИИ С ИСТОРИЕЙ СООБЩЕНИЙ (chat_history)
# ==============================================================================


async def save_chat_message(
    sender_id: int,
    text: str,
    sender_name: str = "Жена",
    source_message_id: Optional[int] = None,
    message_type: str = "text",
    db_path: Optional[str] = None,
) -> None:
    """Сохраняет сообщение в таблицу chat_history для последующего контекстного анализа."""
    created_at = get_now().strftime("%Y-%m-%d %H:%M:%S")

    if is_postgres_configured(db_path):
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO chat_history (sender_id, sender_name, text, created_at, source_message_id, message_type)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                sender_id, sender_name, text, created_at, source_message_id, message_type,
            )
    else:
        target_path = _get_db_path(db_path)
        async with aiosqlite.connect(target_path) as db:
            await db.execute(
                """
                INSERT INTO chat_history (sender_id, sender_name, text, created_at, source_message_id, message_type)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sender_id, sender_name, text, created_at, source_message_id, message_type),
            )
            await db.commit()


async def get_recent_chat_history(
    limit: int = 7,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Получает последние N сообщений в хронологическом порядке (от старых к новым)."""
    if is_postgres_configured(db_path):
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, sender_id, sender_name, text, created_at
                FROM chat_history
                ORDER BY id DESC
                LIMIT $1
                """,
                limit,
            )
            return [dict(r) for r in reversed(rows)]
    else:
        target_path = _get_db_path(db_path)
        async with aiosqlite.connect(target_path) as db:
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


# ==============================================================================
# ОПЕРАЦИИ С ЗАДАЧАМИ (tasks)
# ==============================================================================


async def get_latest_active_task(db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Получает последнюю активную задачу со статусом pending или reminded."""
    query_pg = """
        SELECT id, title, description, deadline, status, created_at, updated_at,
               trello_card_id, trello_card_url, category, location, meeting_url,
               contact_info, caldav_uid, caldav_url
        FROM tasks
        WHERE status IN ('pending', 'reminded')
        ORDER BY id DESC
        LIMIT 1
    """
    if is_postgres_configured(db_path):
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query_pg)
            return dict(row) if row else None
    else:
        target_path = _get_db_path(db_path)
        async with aiosqlite.connect(target_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query_pg) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None


async def get_task_by_id(task_id: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Возвращает задачу по её первичному ключу ID."""
    if is_postgres_configured(db_path):
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, title, description, deadline, status, created_at, updated_at,
                       trello_card_id, trello_card_url, category, location, meeting_url,
                       contact_info, caldav_uid, caldav_url
                FROM tasks
                WHERE id = $1
                """,
                task_id,
            )
            return dict(row) if row else None
    else:
        target_path = _get_db_path(db_path)
        async with aiosqlite.connect(target_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, title, description, deadline, status, created_at, updated_at,
                       trello_card_id, trello_card_url, category, location, meeting_url,
                       contact_info, caldav_uid, caldav_url
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
    category: str = "task",
    location: Optional[str] = None,
    meeting_url: Optional[str] = None,
    contact_info: Optional[str] = None,
    caldav_uid: Optional[str] = None,
    caldav_url: Optional[str] = None,
    trello_card_id: Optional[str] = None,
    trello_card_url: Optional[str] = None,
    db_path: Optional[str] = None,
) -> int:
    """Создает новую задачу/событие в базе данных и возвращает её ID."""
    created_at = get_now().strftime("%Y-%m-%d %H:%M:%S")

    if is_postgres_configured(db_path):
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            task_id = await conn.fetchval(
                """
                INSERT INTO tasks (
                    title, description, deadline, status, created_at, updated_at,
                    category, location, meeting_url, contact_info, caldav_uid, caldav_url,
                    trello_card_id, trello_card_url
                )
                VALUES ($1, $2, $3, 'pending', $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                RETURNING id
                """,
                title, description, deadline, created_at, created_at,
                category, location, meeting_url, contact_info, caldav_uid, caldav_url,
                trello_card_id, trello_card_url,
            )
            return int(task_id)
    else:
        target_path = _get_db_path(db_path)
        async with aiosqlite.connect(target_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO tasks (
                    title, description, deadline, status, created_at, updated_at,
                    category, location, meeting_url, contact_info, caldav_uid, caldav_url,
                    trello_card_id, trello_card_url
                )
                VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title, description, deadline, created_at, created_at,
                    category, location, meeting_url, contact_info, caldav_uid, caldav_url,
                    trello_card_id, trello_card_url,
                ),
            )
            await db.commit()
            return cursor.lastrowid


async def update_task(
    task_id: int,
    title: Optional[str] = None,
    description: Optional[str] = None,
    deadline: Optional[str] = None,
    status: Optional[str] = None,
    trello_card_id: Optional[str] = None,
    trello_card_url: Optional[str] = None,
    category: Optional[str] = None,
    location: Optional[str] = None,
    meeting_url: Optional[str] = None,
    contact_info: Optional[str] = None,
    caldav_uid: Optional[str] = None,
    caldav_url: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Обновляет поля существующей задачи."""
    updated_at = get_now().strftime("%Y-%m-%d %H:%M:%S")

    update_fields: Dict[str, Any] = {"updated_at": updated_at}
    if title is not None:
        update_fields["title"] = title
    if description is not None:
        update_fields["description"] = description
    if deadline is not None:
        update_fields["deadline"] = deadline
    if status is not None:
        update_fields["status"] = status
    if trello_card_id is not None:
        update_fields["trello_card_id"] = trello_card_id
    if trello_card_url is not None:
        update_fields["trello_card_url"] = trello_card_url
    if category is not None:
        update_fields["category"] = category
    if location is not None:
        update_fields["location"] = location
    if meeting_url is not None:
        update_fields["meeting_url"] = meeting_url
    if contact_info is not None:
        update_fields["contact_info"] = contact_info
    if caldav_uid is not None:
        update_fields["caldav_uid"] = caldav_uid
    if caldav_url is not None:
        update_fields["caldav_url"] = caldav_url

    if is_postgres_configured(db_path):
        pool = await get_pg_pool()
        field_strs = [f"{col} = ${i+1}" for i, col in enumerate(update_fields.keys())]
        values = list(update_fields.values())
        values.append(task_id)
        query = f"UPDATE tasks SET {', '.join(field_strs)} WHERE id = ${len(values)}"

        async with pool.acquire() as conn:
            result = await conn.execute(query, *values)
            # Result like "UPDATE 1"
            return result.endswith("1")
    else:
        target_path = _get_db_path(db_path)
        field_strs = [f"{col} = ?" for col in update_fields.keys()]
        values = list(update_fields.values())
        values.append(task_id)
        query = f"UPDATE tasks SET {', '.join(field_strs)} WHERE id = ?"

        async with aiosqlite.connect(target_path) as db:
            cursor = await db.execute(query, values)
            await db.commit()
            return cursor.rowcount > 0


async def update_task_trello_info(
    task_id: int,
    trello_card_id: str,
    trello_card_url: str,
    db_path: Optional[str] = None,
) -> bool:
    """Сохраняет привязку задачи к карточке Trello."""
    return await update_task(
        task_id=task_id,
        trello_card_id=trello_card_id,
        trello_card_url=trello_card_url,
        db_path=db_path,
    )


async def update_task_caldav_info(
    task_id: int,
    caldav_uid: str,
    caldav_url: str,
    db_path: Optional[str] = None,
) -> bool:
    """Сохраняет привязку задачи к событию CalDAV."""
    return await update_task(
        task_id=task_id,
        caldav_uid=caldav_uid,
        caldav_url=caldav_url,
        db_path=db_path,
    )


async def get_task_by_trello_card_id(
    trello_card_id: str,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Возвращает задачу по её ID карточки Trello."""
    if is_postgres_configured(db_path):
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, title, description, deadline, status, created_at, updated_at,
                       trello_card_id, trello_card_url, category, location, meeting_url,
                       contact_info, caldav_uid, caldav_url
                FROM tasks
                WHERE trello_card_id = $1
                ORDER BY id DESC
                LIMIT 1
                """,
                trello_card_id,
            )
            return dict(row) if row else None
    else:
        target_path = _get_db_path(db_path)
        async with aiosqlite.connect(target_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, title, description, deadline, status, created_at, updated_at,
                       trello_card_id, trello_card_url, category, location, meeting_url,
                       contact_info, caldav_uid, caldav_url
                FROM tasks
                WHERE trello_card_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (trello_card_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None


async def find_active_task_for_trello_card(
    card_id: str,
    card_title: str,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Ищет активную задачу для карточки Trello."""
    if is_postgres_configured(db_path):
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            # 1. По trello_card_id
            if card_id:
                row = await conn.fetchrow(
                    """
                    SELECT id, title, description, deadline, status, created_at, updated_at,
                           trello_card_id, trello_card_url, category, location, meeting_url,
                           contact_info, caldav_uid, caldav_url
                    FROM tasks
                    WHERE trello_card_id = $1 AND status IN ('pending', 'reminded')
                    ORDER BY id DESC LIMIT 1
                    """,
                    card_id,
                )
                if row:
                    return dict(row)

            # 2. По ID в заголовке (#12)
            if card_title:
                match = re.search(r"#(\d+)", card_title)
                if match:
                    candidate_id = int(match.group(1))
                    row = await conn.fetchrow(
                        """
                        SELECT id, title, description, deadline, status, created_at, updated_at,
                               trello_card_id, trello_card_url, category, location, meeting_url,
                               contact_info, caldav_uid, caldav_url
                        FROM tasks
                        WHERE id = $1 AND status IN ('pending', 'reminded')
                        LIMIT 1
                        """,
                        candidate_id,
                    )
                    if row:
                        return dict(row)

                clean_title = card_title.strip()
                row = await conn.fetchrow(
                    """
                    SELECT id, title, description, deadline, status, created_at, updated_at,
                           trello_card_id, trello_card_url, category, location, meeting_url,
                           contact_info, caldav_uid, caldav_url
                    FROM tasks
                    WHERE title = $1 AND status IN ('pending', 'reminded')
                    ORDER BY id DESC LIMIT 1
                    """,
                    clean_title,
                )
                if row:
                    return dict(row)
        return None
    else:
        target_path = _get_db_path(db_path)
        async with aiosqlite.connect(target_path) as db:
            db.row_factory = aiosqlite.Row
            if card_id:
                async with db.execute(
                    """
                    SELECT id, title, description, deadline, status, created_at, updated_at,
                           trello_card_id, trello_card_url, category, location, meeting_url,
                           contact_info, caldav_uid, caldav_url
                    FROM tasks
                    WHERE trello_card_id = ? AND status IN ('pending', 'reminded')
                    ORDER BY id DESC LIMIT 1
                    """,
                    (card_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return dict(row)

            if card_title:
                match = re.search(r"#(\d+)", card_title)
                if match:
                    candidate_id = int(match.group(1))
                    async with db.execute(
                        """
                        SELECT id, title, description, deadline, status, created_at, updated_at,
                               trello_card_id, trello_card_url, category, location, meeting_url,
                               contact_info, caldav_uid, caldav_url
                        FROM tasks
                        WHERE id = ? AND status IN ('pending', 'reminded')
                        LIMIT 1
                        """,
                        (candidate_id,),
                    ) as cursor:
                        row = await cursor.fetchone()
                        if row:
                            return dict(row)

                clean_title = card_title.strip()
                async with db.execute(
                    """
                    SELECT id, title, description, deadline, status, created_at, updated_at,
                           trello_card_id, trello_card_url, category, location, meeting_url,
                           contact_info, caldav_uid, caldav_url
                    FROM tasks
                    WHERE title = ? AND status IN ('pending', 'reminded')
                    ORDER BY id DESC LIMIT 1
                    """,
                    (clean_title,),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return dict(row)
        return None


async def update_task_status(
    task_id: int,
    status: str,
    db_path: Optional[str] = None,
) -> None:
    """Обновляет только статус задачи (pending / reminded / completed)."""
    await update_task(task_id=task_id, status=status, db_path=db_path)
    logger.info("Статус задачи #%d обновлен на '%s'.", task_id, status)


async def get_pending_tasks_with_deadline(
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Получает все незавершенные задачи с установленным дедлайном для APScheduler."""
    query = """
        SELECT id, title, description, deadline, status, created_at, category, location, meeting_url, contact_info
        FROM tasks
        WHERE status = 'pending' AND deadline IS NOT NULL
    """
    if is_postgres_configured(db_path):
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(query)
            return [dict(row) for row in rows]
    else:
        target_path = _get_db_path(db_path)
        async with aiosqlite.connect(target_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]


async def get_active_tasks(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Получает список всех актуальных задач для вывода пользователю."""
    query = """
        SELECT id, title, description, deadline, status, created_at, trello_card_id, trello_card_url,
               category, location, meeting_url, contact_info, caldav_uid, caldav_url
        FROM tasks
        WHERE status IN ('pending', 'reminded')
        ORDER BY id DESC
    """
    if is_postgres_configured(db_path):
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(query)
            return [dict(row) for row in rows]
    else:
        target_path = _get_db_path(db_path)
        async with aiosqlite.connect(target_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]


async def get_tasks(
    status: Optional[str] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 10,
    offset: int = 0,
    sort_by: str = "id",
    sort_order: str = "desc",
    db_path: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Возвращает список задач с пагинацией, фильтрацией и общим количеством.
    status: 'all', 'active' (pending, reminded), 'completed', 'pending', 'reminded'
    category: фильтр по категории (например, 'task', 'calendar')
    search: поиск по названию, описанию, месту и контакту
    limit: количество задач на страницу (1..100)
    offset: смещение
    sort_by: поле сортировки ('id', 'deadline', 'created_at', 'updated_at', 'status', 'title')
    sort_order: 'desc' или 'asc'
    """
    valid_sort_fields = {"id", "deadline", "created_at", "updated_at", "status", "category", "title"}
    field = sort_by if sort_by in valid_sort_fields else "id"
    direction = "ASC" if str(sort_order).lower() == "asc" else "DESC"
    safe_limit = max(1, min(int(limit), 100))
    safe_offset = max(0, int(offset))

    if is_postgres_configured(db_path):
        where_clauses = []
        params = []
        p_idx = 1

        if status:
            s = status.strip().lower()
            if s == "active":
                where_clauses.append("status IN ('pending', 'reminded')")
            elif s != "all":
                where_clauses.append(f"status = ${p_idx}")
                params.append(s)
                p_idx += 1

        if category and category.strip().lower() != "all":
            where_clauses.append(f"category = ${p_idx}")
            params.append(category.strip())
            p_idx += 1

        if search and search.strip():
            term = f"%{search.strip()}%"
            where_clauses.append(
                f"(title ILIKE ${p_idx} OR description ILIKE ${p_idx+1} OR location ILIKE ${p_idx+2} OR contact_info ILIKE ${p_idx+3})"
            )
            params.extend([term, term, term, term])
            p_idx += 4

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        count_query = f"SELECT COUNT(*) FROM tasks {where_sql}"
        select_query = f"""
            SELECT id, title, description, deadline, status, created_at, updated_at,
                   trello_card_id, trello_card_url, category, location, meeting_url,
                   contact_info, caldav_uid, caldav_url
            FROM tasks
            {where_sql}
            ORDER BY {field} {direction}
            LIMIT ${p_idx} OFFSET ${p_idx+1}
        """
        select_params = list(params) + [safe_limit, safe_offset]

        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval(count_query, *params)
            rows = await conn.fetch(select_query, *select_params)
            return [dict(row) for row in rows], int(total or 0)
    else:
        target_path = _get_db_path(db_path)
        where_clauses = []
        params = []

        if status:
            s = status.strip().lower()
            if s == "active":
                where_clauses.append("status IN ('pending', 'reminded')")
            elif s != "all":
                where_clauses.append("status = ?")
                params.append(s)

        if category and category.strip().lower() != "all":
            where_clauses.append("category = ?")
            params.append(category.strip())

        if search and search.strip():
            term = f"%{search.strip().lower()}%"
            where_clauses.append(
                "(lower(title) LIKE ? OR lower(COALESCE(description, '')) LIKE ? OR lower(COALESCE(location, '')) LIKE ? OR lower(COALESCE(contact_info, '')) LIKE ?)"
            )
            params.extend([term, term, term, term])

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        count_query = f"SELECT COUNT(*) FROM tasks {where_sql}"
        select_query = f"""
            SELECT id, title, description, deadline, status, created_at, updated_at,
                   trello_card_id, trello_card_url, category, location, meeting_url,
                   contact_info, caldav_uid, caldav_url
            FROM tasks
            {where_sql}
            ORDER BY {field} {direction}
            LIMIT ? OFFSET ?
        """
        select_params = list(params) + [safe_limit, safe_offset]

        async with aiosqlite.connect(target_path) as db:
            await db.create_function("lower", 1, lambda s: s.lower() if s is not None else "")
            db.row_factory = aiosqlite.Row
            async with db.execute(count_query, params) as cursor:
                count_row = await cursor.fetchone()
                total = count_row[0] if count_row else 0
            async with db.execute(select_query, select_params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows], int(total or 0)


async def get_task_stats(db_path: Optional[str] = None) -> Dict[str, int]:
    """Возвращает агрегированную статистику по задачам: всего, активных, выполненных, в календаре."""
    query = """
        SELECT
            COUNT(*) as total,
            COALESCE(SUM(CASE WHEN status IN ('pending', 'reminded') THEN 1 ELSE 0 END), 0) as active,
            COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) as pending,
            COALESCE(SUM(CASE WHEN status = 'reminded' THEN 1 ELSE 0 END), 0) as reminded,
            COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0) as completed,
            COALESCE(SUM(CASE WHEN caldav_uid IS NOT NULL AND caldav_uid != '' THEN 1 ELSE 0 END), 0) as in_calendar
        FROM tasks
    """
    default_stats = {
        "total": 0,
        "active": 0,
        "pending": 0,
        "reminded": 0,
        "completed": 0,
        "in_calendar": 0,
    }
    if is_postgres_configured(db_path):
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query)
            if not row:
                return default_stats
            return {k: int(row[k] or 0) for k in default_stats}
    else:
        target_path = _get_db_path(db_path)
        async with aiosqlite.connect(target_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return default_stats
                return {k: int(row[k] or 0) for k in default_stats}


# ==============================================================================
# ОПЕРАЦИИ С ГОЛОСОВЫМИ СООБЩЕНИЯМИ (voice_messages)
# ==============================================================================


async def init_voice_db(db_or_path: Any = None) -> None:
    """Инициализация таблицы голосовых сообщений."""
    if isinstance(db_or_path, str):
        await init_db(db_or_path)
    elif db_or_path is not None and hasattr(db_or_path, "execute"):
        # Передан существующий aiosqlite connection (для обратной совместимости)
        from voice.db import _init_voice_sqlite_conn
        await _init_voice_sqlite_conn(db_or_path)
    else:
        await init_db()


async def get_voice_message(
    chat_id: int,
    telegram_message_id: int,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Получает запись голосового сообщения по chat_id и telegram_message_id."""
    if is_postgres_configured(db_path):
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, telegram_message_id, chat_id, business_connection_id,
                       sender_id, telegram_file_id, audio_path, transcript,
                       summary, category, processing_status, error_message,
                       created_at, processed_at
                FROM voice_messages
                WHERE chat_id = $1 AND telegram_message_id = $2
                """,
                chat_id, telegram_message_id,
            )
            return dict(row) if row else None
    else:
        target_path = _get_db_path(db_path)
        async with aiosqlite.connect(target_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, telegram_message_id, chat_id, business_connection_id,
                       sender_id, telegram_file_id, audio_path, transcript,
                       summary, category, processing_status, error_message,
                       created_at, processed_at
                FROM voice_messages
                WHERE chat_id = ? AND telegram_message_id = ?
                """,
                (chat_id, telegram_message_id),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None


async def create_voice_message(
    chat_id: int,
    telegram_message_id: int,
    sender_id: int,
    telegram_file_id: Optional[str] = None,
    business_connection_id: Optional[str] = None,
    audio_path: Optional[str] = None,
    db_path: Optional[str] = None,
) -> int:
    """
    Создает новую запись голосового сообщения со статусом 'pending'.
    В случае повторной отправки (idempotency) возвращает ID существующей записи.
    """
    existing = await get_voice_message(chat_id, telegram_message_id, db_path)
    if existing:
        return existing["id"]

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if is_postgres_configured(db_path):
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            v_id = await conn.fetchval(
                """
                INSERT INTO voice_messages (
                    telegram_message_id, chat_id, business_connection_id,
                    sender_id, telegram_file_id, audio_path,
                    processing_status, created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7)
                ON CONFLICT (chat_id, telegram_message_id) DO NOTHING
                RETURNING id
                """,
                telegram_message_id, chat_id, business_connection_id,
                sender_id, telegram_file_id, audio_path, created_at,
            )
            if v_id is not None:
                return int(v_id)
            # Если вернулся None из-за конфликта уникальности, получаем существующий
            ex = await get_voice_message(chat_id, telegram_message_id, db_path)
            return ex["id"] if ex else 0
    else:
        target_path = _get_db_path(db_path)
        async with aiosqlite.connect(target_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO voice_messages (
                    telegram_message_id, chat_id, business_connection_id,
                    sender_id, telegram_file_id, audio_path,
                    processing_status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    telegram_message_id, chat_id, business_connection_id,
                    sender_id, telegram_file_id, audio_path, created_at,
                ),
            )
            await db.commit()
            return cursor.lastrowid


async def update_voice_message(
    chat_id: int,
    telegram_message_id: int,
    status: Optional[str] = None,
    audio_path: Optional[str] = None,
    transcript: Optional[str] = None,
    summary: Optional[str] = None,
    category: Optional[str] = None,
    error_message: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Обновляет статус и данные голосового сообщения."""
    update_fields: Dict[str, Any] = {}

    if status is not None:
        update_fields["processing_status"] = status
        if status in ("completed", "failed"):
            update_fields["processed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if audio_path is not None:
        update_fields["audio_path"] = audio_path
    if transcript is not None:
        update_fields["transcript"] = transcript
    if summary is not None:
        update_fields["summary"] = summary
    if category is not None:
        update_fields["category"] = category
    if error_message is not None:
        update_fields["error_message"] = error_message

    if not update_fields:
        return False

    if is_postgres_configured(db_path):
        pool = await get_pg_pool()
        field_strs = [f"{col} = ${i+1}" for i, col in enumerate(update_fields.keys())]
        values = list(update_fields.values())
        values.extend([chat_id, telegram_message_id])
        query = f"UPDATE voice_messages SET {', '.join(field_strs)} WHERE chat_id = ${len(values)-1} AND telegram_message_id = ${len(values)}"

        async with pool.acquire() as conn:
            result = await conn.execute(query, *values)
            return result.endswith("1")
    else:
        target_path = _get_db_path(db_path)
        field_strs = [f"{col} = ?" for col in update_fields.keys()]
        values = list(update_fields.values())
        values.extend([chat_id, telegram_message_id])
        query = f"UPDATE voice_messages SET {', '.join(field_strs)} WHERE chat_id = ? AND telegram_message_id = ?"

        async with aiosqlite.connect(target_path) as db:
            cursor = await db.execute(query, values)
            await db.commit()
            return cursor.rowcount > 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        print("Запуск миграции SQLite -> PostgreSQL...")
        asyncio.run(migrate_sqlite_to_postgres())
        print("Миграция завершена.")
