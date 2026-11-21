import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any, Dict, Optional
import uuid

import caldav
import icalendar

from calendar_sync.config import (
    CALDAV_CALENDAR_NAME,
    CALDAV_ENABLED,
    CALDAV_PASSWORD,
    CALDAV_URL,
    CALDAV_USERNAME,
)
from tz_utils import BOT_TIMEZONE, ensure_tz

logger = logging.getLogger("FamilyTasksBot.CalendarSync")


class CalendarSyncService:
    """
    Сервис синхронизации событий с Apple Calendar / CalDAV.
    Позволяет создавать события календаря с указанием даты, времени,
    места проведения, ссылки на видеовстречу и контактных данных.
    """

    def __init__(
        self,
        url: str = CALDAV_URL,
        username: str = CALDAV_USERNAME,
        password: str = CALDAV_PASSWORD,
        calendar_name: str = CALDAV_CALENDAR_NAME,
        enabled: bool = CALDAV_ENABLED,
    ):
        self.url = url.strip()
        self.username = username.strip()
        self.password = password.strip()
        self.calendar_name = calendar_name.strip()
        self.enabled = enabled

    @property
    def is_enabled(self) -> bool:
        """Интеграция активна только если включена и заданы учетные данные CalDAV."""
        return bool(self.enabled and self.url and self.username and self.password)

    def _build_ical_event(
        self,
        title: str,
        start_dt: datetime,
        end_dt: datetime,
        location: Optional[str] = None,
        description: Optional[str] = None,
        meeting_url: Optional[str] = None,
        contact_info: Optional[str] = None,
        event_uid: Optional[str] = None,
    ) -> str:
        """Формирует строковое iCalendar VCALENDAR представление события."""
        uid = event_uid or str(uuid.uuid4())
        cal = icalendar.Calendar()
        cal.add("prodid", "-//PersonalFamilyTasksBot//RU")
        cal.add("version", "2.0")

        event = icalendar.Event()
        event.add("uid", uid)
        event.add("summary", title)
        event.add("dtstart", ensure_tz(start_dt))
        event.add("dtend", ensure_tz(end_dt))
        event.add("status", "CONFIRMED")

        if location:
            event.add("location", location)

        desc_parts = []
        if description:
            desc_parts.append(description)
        if contact_info:
            desc_parts.append(f"📞 Контакт: {contact_info}")
        if meeting_url:
            desc_parts.append(f"🔗 Ссылка для созвона: {meeting_url}")
        if desc_parts:
            event.add("description", "\n".join(desc_parts))

        if meeting_url:
            event.add("url", meeting_url)

        cal.add_component(event)
        return cal.to_ical().decode("utf-8")

    def _sync_create_event(
        self,
        title: str,
        start_dt: datetime,
        end_dt: Optional[datetime] = None,
        location: Optional[str] = None,
        description: Optional[str] = None,
        meeting_url: Optional[str] = None,
        contact_info: Optional[str] = None,
    ) -> Optional[Dict[str, str]]:
        """Синхронный вызов CalDAV API для создания события."""
        if not self.is_enabled:
            return None

        actual_start = ensure_tz(start_dt)
        actual_end = ensure_tz(end_dt) if end_dt else (actual_start + timedelta(hours=1))

        event_uid = str(uuid.uuid4())
        ical_str = self._build_ical_event(
            title=title,
            start_dt=actual_start,
            end_dt=actual_end,
            location=location,
            description=description,
            meeting_url=meeting_url,
            contact_info=contact_info,
            event_uid=event_uid,
        )

        try:
            client = caldav.DAVClient(
                url=self.url,
                username=self.username,
                password=self.password,
            )
            principal = client.principal()
            calendars = principal.calendars()
            if not calendars:
                logger.error("В CalDAV не найдено ни одного календаря для учетной записи %s.", self.username)
                return None

            target_cal = calendars[0]
            if self.calendar_name:
                for c in calendars:
                    c_name = getattr(c, "name", "") or ""
                    if c_name.strip().lower() == self.calendar_name.lower():
                        target_cal = c
                        break

            saved_event = target_cal.save_event(ical_str)
            event_url = str(getattr(saved_event, "url", "") or "")
            logger.info(
                "✅ Событие '%s' успешно сохранено в Apple/CalDAV календарь: UID=%s URL=%s",
                title,
                event_uid,
                event_url,
            )
            return {
                "uid": event_uid,
                "url": event_url,
            }
        except Exception as e:
            logger.error("Ошибка при сохранении события в CalDAV (%s): %s", self.url, e)
            return None

    async def create_event(
        self,
        title: str,
        start_dt: datetime,
        end_dt: Optional[datetime] = None,
        location: Optional[str] = None,
        description: Optional[str] = None,
        meeting_url: Optional[str] = None,
        contact_info: Optional[str] = None,
    ) -> Optional[Dict[str, str]]:
        """
        Асинхронно создает событие в CalDAV календаре, выполняя сетевые запросы
        в отдельном потоке во избежание блокировки asyncio event loop.
        """
        if not self.is_enabled:
            return None

        return await asyncio.to_thread(
            self._sync_create_event,
            title=title,
            start_dt=start_dt,
            end_dt=end_dt,
            location=location,
            description=description,
            meeting_url=meeting_url,
            contact_info=contact_info,
        )
