import os
import re
from typing import List, Optional


def parse_list_names(raw: Optional[str], separator: str = ",") -> List[str]:
    """
    Разбирает строку с именами колонок/списков, разделенных разделителем (по умолчанию запятая),
    очищая каждый элемент от пробелов и исключая пустые строки.
    Поддерживает как кириллицу, так и латиницу.
    """
    if not raw:
        return []
    # Если передан кастомный разделитель, экранируем его для re.split
    parts = re.split(re.escape(separator), raw)
    cleaned = [p.strip() for p in parts if p.strip()]
    return cleaned


# ==============================================================================
# КОНФИГУРАЦИЯ ИНТЕГРАЦИИ С TRELLO
# ==============================================================================

# Учетные данные Trello API
TRELLO_API_KEY: str = os.getenv("TRELLO_API_KEY", "").strip()
TRELLO_API_SECRET: str = os.getenv("TRELLO_API_SECRET", "").strip()
TRELLO_TOKEN: str = os.getenv("TRELLO_TOKEN", "").strip()

# Если токен не задан явно, но задан секрет и выглядит как токен (обычно 64 шестнадцатеричных символа)
if not TRELLO_TOKEN and TRELLO_API_SECRET and len(TRELLO_API_SECRET) >= 64:
    TRELLO_TOKEN = TRELLO_API_SECRET

# ID или короткий идентификатор доски Trello
TRELLO_BOARD_ID: str = os.getenv("TRELLO_BOARD_ID", "").strip()

# Разделитель для имен списков в переменных окружения
TRELLO_LIST_SEPARATOR: str = os.getenv("TRELLO_LIST_SEPARATOR", ",").strip() or ","

# Сырые списки имен колонок
_DEFAULT_INBOX_LISTS = "Inbox, Входящие, Новые, To Do"
_DEFAULT_DONE_LISTS = "Done, Завершено, Выполнено, Готово, Сделано"

TRELLO_INBOX_LISTS_RAW: str = os.getenv("TRELLO_INBOX_LISTS", _DEFAULT_INBOX_LISTS)
TRELLO_DONE_LISTS_RAW: str = os.getenv("TRELLO_DONE_LISTS", _DEFAULT_DONE_LISTS)

# Распарсенные списки имен
TRELLO_INBOX_LISTS: List[str] = parse_list_names(TRELLO_INBOX_LISTS_RAW, TRELLO_LIST_SEPARATOR)
TRELLO_DONE_LISTS: List[str] = parse_list_names(TRELLO_DONE_LISTS_RAW, TRELLO_LIST_SEPARATOR)

# Флаг включения: явный флаг или автоматическое включение при наличии ключа и токена
_raw_enabled = os.getenv("TRELLO_ENABLED", "").strip().lower()
if _raw_enabled in ("true", "1", "yes"):
    TRELLO_ENABLED: bool = True
elif _raw_enabled in ("false", "0", "no"):
    TRELLO_ENABLED: bool = False
else:
    TRELLO_ENABLED: bool = bool(TRELLO_API_KEY and (TRELLO_TOKEN or TRELLO_API_SECRET))

# Интервал систематического сканирования (fallback polling) в секундах
TRELLO_POLL_INTERVAL_SECONDS: int = int(os.getenv("TRELLO_POLL_INTERVAL_SECONDS", "60"))

# Настройки входящего вебхука (callback)
_raw_webhook_enabled = os.getenv("TRELLO_WEBHOOK_ENABLED", "true").strip().lower()
TRELLO_WEBHOOK_ENABLED: bool = _raw_webhook_enabled not in ("false", "0", "no")

TRELLO_WEBHOOK_HOST: str = os.getenv("TRELLO_WEBHOOK_HOST", "0.0.0.0").strip()
TRELLO_WEBHOOK_PORT: int = int(os.getenv("TRELLO_WEBHOOK_PORT", "8080"))
TRELLO_WEBHOOK_PATH: str = os.getenv("TRELLO_WEBHOOK_PATH", "/trello/webhook").strip()
if not TRELLO_WEBHOOK_PATH.startswith("/"):
    TRELLO_WEBHOOK_PATH = f"/{TRELLO_WEBHOOK_PATH}"

# Публичный callback URL (например, https://example.ngrok-free.app/trello/webhook)
TRELLO_WEBHOOK_CALLBACK_URL: Optional[str] = os.getenv("TRELLO_WEBHOOK_CALLBACK_URL", "").strip() or None
