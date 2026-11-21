import os

# ==============================================================================
# КОНФИГУРАЦИЯ СИНХРОНИЗАЦИИ С APPLE CALENDAR ЧЕРЕЗ CALDAV
# ==============================================================================

# Флаг включения интеграции с календарем CalDAV
_raw_caldav_enabled = os.getenv("CALDAV_ENABLED", "false").strip().lower()
CALDAV_ENABLED: bool = _raw_caldav_enabled in ("true", "1", "yes")

# URL CalDAV сервера (для Apple iCloud по умолчанию https://caldav.icloud.com/)
CALDAV_URL: str = os.getenv("CALDAV_URL", "https://caldav.icloud.com/").strip()

# Apple ID / логин учетной записи CalDAV (например, user@icloud.com)
CALDAV_USERNAME: str = os.getenv("CALDAV_USERNAME", "").strip()

# Пароль приложения (App-Specific Password для iCloud, создается на appleid.apple.com)
CALDAV_PASSWORD: str = os.getenv("CALDAV_PASSWORD", "").strip()

# Имя календаря по умолчанию (если не указано, используется первый доступный календарь)
CALDAV_CALENDAR_NAME: str = os.getenv("CALDAV_CALENDAR_NAME", "").strip()
