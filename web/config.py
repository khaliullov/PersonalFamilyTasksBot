import os
from typing import Optional


def normalize_prefix(prefix: Optional[str]) -> str:
    """
    Нормализует префикс маршрутизации веб-интерфейса и API.
    Например: '/pftb' -> '/pftb', 'pftb/' -> '/pftb', '' -> ''.
    """
    if not prefix:
        return ""
    p = prefix.strip()
    if not p or p == "/":
        return ""
    if not p.startswith("/"):
        p = f"/{p}"
    return p.rstrip("/")


_raw_web_enabled = os.getenv("WEB_ENABLED", "true").strip().lower()
WEB_ENABLED: bool = _raw_web_enabled not in ("false", "0", "no")

WEB_HOST: str = os.getenv("WEB_HOST", "0.0.0.0").strip()
WEB_PORT: int = int(os.getenv("WEB_PORT", os.getenv("TRELLO_WEBHOOK_PORT", "8080")))

# Основной префикс для всех маршрутов приложения (по умолчанию /pftb)
WEB_PREFIX: str = normalize_prefix(os.getenv("WEB_PREFIX", "/pftb"))

# Префикс REST API (по умолчанию /pftb/api/v1)
API_V1_PREFIX: str = f"{WEB_PREFIX}/api/v1" if WEB_PREFIX else "/api/v1"

# Базовый путь для Web UI (по умолчанию /pftb/)
WEB_UI_PATH: str = f"{WEB_PREFIX}/" if WEB_PREFIX else "/"

from aiohttp import web

# Ключи конфигурации для aiohttp Application
DB_PATH_KEY = web.AppKey("db_path", Optional[str])
PREFIX_KEY = web.AppKey("prefix", str)
API_PREFIX_KEY = web.AppKey("api_prefix", str)
