from datetime import datetime, timedelta, timezone
import logging
import os
import re
from typing import Optional, Union
import zoneinfo

logger = logging.getLogger("FamilyTasksBot.Timezone")

# Часовой пояс по умолчанию: UTC+4
DEFAULT_TIMEZONE_STR: str = "UTC+4"


def parse_timezone(tz_str: Optional[str]) -> timezone:
    """
    Парсит строку часового пояса и возвращает объект timezone / ZoneInfo.
    Поддерживает форматы:
    - IANA имена зон: 'Asia/Dubai', 'Europe/Samara', 'Europe/Moscow', 'UTC'
    - Относительные смещения: 'UTC+4', 'UTC+04:00', '+04:00', '+4', 'GMT+4', 'UTC-5', '-03:00'
    При пустой строке или ошибке возвращает часовой пояс по умолчанию (UTC+4).
    """
    if not tz_str or not tz_str.strip():
        return timezone(timedelta(hours=4))

    clean_str = tz_str.strip()

    # 1. Попытка через IANA ZoneInfo
    try:
        return zoneinfo.ZoneInfo(clean_str)
    except Exception:
        pass

    # 2. Попытка через регулярное выражение смещения UTC/GMT
    pattern = r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$"
    match = re.match(pattern, clean_str, re.IGNORECASE)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        hours = int(match.group(2))
        minutes = int(match.group(3)) if match.group(3) else 0
        return timezone(sign * timedelta(hours=hours, minutes=minutes))

    logger.warning("Не удалось распознать часовой пояс '%s'. Используется UTC+4 по умолчанию.", tz_str)
    return timezone(timedelta(hours=4))


# Считываем конфигурацию из переменной окружения TIMEZONE (по умолчанию UTC+4)
TIMEZONE_STR: str = os.getenv("TIMEZONE", DEFAULT_TIMEZONE_STR).strip() or DEFAULT_TIMEZONE_STR
BOT_TIMEZONE: Union[timezone, zoneinfo.ZoneInfo] = parse_timezone(TIMEZONE_STR)


def get_now() -> datetime:
    """Возвращает текущую дату и время в настроенном часовом поясе BOT_TIMEZONE."""
    return datetime.now(BOT_TIMEZONE)


def ensure_tz(dt: datetime) -> datetime:
    """
    Гарантирует, что объект datetime имеет часовой пояс BOT_TIMEZONE.
    Если объект наивный (без tzinfo), привязывает BOT_TIMEZONE.
    Если уже осведомленный (aware), конвертирует в BOT_TIMEZONE.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BOT_TIMEZONE)
    return dt.astimezone(BOT_TIMEZONE)


def format_dt(dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Форматирует дату в строковый формат в часовом поясе бота."""
    return ensure_tz(dt).strftime(fmt)
