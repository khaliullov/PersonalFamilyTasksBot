"""
Пакет интеграции с Apple Calendar через CalDAV.
"""

from calendar_sync.config import (
    CALDAV_CALENDAR_NAME,
    CALDAV_ENABLED,
    CALDAV_PASSWORD,
    CALDAV_URL,
    CALDAV_USERNAME,
)
from calendar_sync.service import CalendarSyncService

__all__ = [
    "CalendarSyncService",
    "CALDAV_ENABLED",
    "CALDAV_URL",
    "CALDAV_USERNAME",
    "CALDAV_PASSWORD",
    "CALDAV_CALENDAR_NAME",
]
