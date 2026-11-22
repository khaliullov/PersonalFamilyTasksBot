import asyncio
import json
import logging
from typing import Any, Callable, Coroutine, Dict, Optional

from aiohttp import web

from trello.config import (
    TRELLO_API_SECRET,
    TRELLO_WEBHOOK_CALLBACK_URL,
    TRELLO_WEBHOOK_PATH,
)
from trello.webhook import verify_trello_webhook_signature
from web.api import (
    api_add_to_calendar,
    api_complete_task,
    api_get_stats,
    api_get_task_detail,
    api_get_tasks,
    api_health,
)
from web.config import (
    API_PREFIX_KEY,
    API_V1_PREFIX,
    DB_PATH_KEY,
    PREFIX_KEY,
    WEB_HOST,
    WEB_PORT,
    WEB_PREFIX,
    WEB_UI_PATH,
)
from web.ui import ui_index_handler

logger = logging.getLogger("FamilyTasksBot.WebServer")


class WebServer:
    """
    Единый Web-сервер приложения:
    - Обслуживает панель управления Web UI (/pftb/)
    - Предоставляет REST API (/pftb/api/v1/...)
    - Принимает внешние callbacks (вебхуки Trello)
    - Обеспечивает настраиваемый префикс (WEB_PREFIX) для изоляции нескольких ботов на одном домене.
    """

    def __init__(
        self,
        host: str = WEB_HOST,
        port: int = WEB_PORT,
        prefix: str = WEB_PREFIX,
        db_path: Optional[str] = None,
        trello_event_handler: Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]] = None,
        trello_secret: Optional[str] = TRELLO_API_SECRET,
        trello_callback_url: Optional[str] = TRELLO_WEBHOOK_CALLBACK_URL,
    ):
        self.host = host
        self.port = port
        self.prefix = prefix
        self.api_prefix = f"{self.prefix}/api/v1" if self.prefix else "/api/v1"
        self.ui_path = f"{self.prefix}/" if self.prefix else "/"
        self.db_path = db_path

        self.trello_event_handler = trello_event_handler
        self.trello_secret = trello_secret.strip() if trello_secret else None
        self.trello_callback_url = trello_callback_url.strip() if trello_callback_url else None

        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._is_running = False

    @property
    def is_running(self) -> bool:
        return self._is_running

    def set_trello_handler(
        self,
        handler: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]],
        secret: Optional[str] = None,
        callback_url: Optional[str] = None,
    ) -> None:
        """Регистрирует или обновляет обработчик входящих вебхуков Trello."""
        self.trello_event_handler = handler
        if secret is not None:
            self.trello_secret = secret.strip() if secret else None
        if callback_url is not None:
            self.trello_callback_url = callback_url.strip() if callback_url else None

    async def _handle_root_redirect(self, request: web.Request) -> web.Response:
        """Перенаправление с корня / на префикс приложения, например /pftb/."""
        target = self.ui_path
        return web.HTTPFound(location=target)

    async def _handle_ui_prefix_redirect(self, request: web.Request) -> web.Response:
        """Перенаправление с /pftb на /pftb/ (со слэшем)."""
        return web.HTTPFound(location=self.ui_path)

    # --------------------------------------------------------------------------
    # Обработчики вебхука Trello (встроены в общий сервер)
    # --------------------------------------------------------------------------

    async def _handle_trello_head(self, request: web.Request) -> web.Response:
        """Trello отправляет HEAD-запрос для валидации доступности callback URL."""
        logger.info("Получен HEAD-запрос проверки вебхука от Trello: %s", request.path)
        return web.Response(status=200)

    async def _handle_trello_get(self, request: web.Request) -> web.Response:
        """Health-check эндпоинт для вебхука Trello."""
        return web.Response(text="PFTB Trello Webhook Endpoint is running (OK).", status=200)

    async def _handle_trello_post(self, request: web.Request) -> web.Response:
        """Прием и обработка полезной нагрузки Trello."""
        raw_body = await request.text()
        signature_header = request.headers.get("X-Trello-Webhook")

        if self.trello_secret and self.trello_callback_url:
            if signature_header:
                is_valid = verify_trello_webhook_signature(
                    request_body=raw_body,
                    callback_url=self.trello_callback_url,
                    secret=self.trello_secret,
                    header_signature=signature_header,
                )
                if not is_valid:
                    logger.warning("Подпись X-Trello-Webhook не прошла проверку: %s", signature_header)
            else:
                logger.warning("Запрос к Trello webhook получен без заголовка X-Trello-Webhook.")

        try:
            data = json.loads(raw_body)
        except json.JSONDecodeError as e:
            logger.warning("Некорректный JSON в теле вебхука Trello: %s", e)
            return web.Response(text="Invalid JSON", status=400)

        if self.trello_event_handler:
            try:
                asyncio.create_task(self._safe_dispatch_trello(data))
            except Exception as e:
                logger.error("Ошибка при постановке задачи обработки события Trello: %s", e)

        return web.Response(text="OK", status=200)

    async def _safe_dispatch_trello(self, data: Dict[str, Any]) -> None:
        try:
            if self.trello_event_handler:
                await self.trello_event_handler(data)
        except Exception as e:
            logger.exception("Исключение в обработчике события Trello webhook: %s", e)

    def create_app(self) -> web.Application:
        """Создает и настраивает экземпляр aiohttp приложения со всеми маршрутами."""
        app = web.Application()
        app[DB_PATH_KEY] = self.db_path
        app[PREFIX_KEY] = self.prefix
        app[API_PREFIX_KEY] = self.api_prefix

        # 1. Корневые редиректы (если задан префикс)
        if self.prefix:
            app.router.add_get("/", self._handle_root_redirect)
            app.router.add_get(self.prefix, self._handle_ui_prefix_redirect)

        # 2. Web UI (Морда)
        app.router.add_get(self.ui_path, ui_index_handler)

        # 3. REST API маршруты
        app.router.add_get(f"{self.api_prefix}/health", api_health)
        app.router.add_get(f"{self.api_prefix}/stats", api_get_stats)
        app.router.add_get(f"{self.api_prefix}/tasks", api_get_tasks)
        app.router.add_get(f"{self.api_prefix}/tasks/{{id}}", api_get_task_detail)
        app.router.add_post(f"{self.api_prefix}/tasks/{{id}}/complete", api_complete_task)
        app.router.add_post(f"{self.api_prefix}/tasks/{{id}}/to_calendar", api_add_to_calendar)

        # 4. Webhook маршруты для Trello
        # Поддерживаем как путь внутри префикса, так и legacy путь TRELLO_WEBHOOK_PATH
        trello_paths = set()
        if self.prefix:
            trello_paths.add(f"{self.prefix}/trello/webhook")
            trello_paths.add(f"{self.prefix}{TRELLO_WEBHOOK_PATH}")
        trello_paths.add(TRELLO_WEBHOOK_PATH)

        for path in trello_paths:
            app.router.add_route("HEAD", path, self._handle_trello_head)
            app.router.add_route("GET", path, self._handle_trello_get)
            app.router.add_route("POST", path, self._handle_trello_post)

        return app

    async def start(self) -> bool:
        """Запускает HTTP-сервер."""
        if self._is_running:
            return True

        self._app = self.create_app()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)

        try:
            await self._site.start()
            self._is_running = True
            logger.info(
                "🚀 Единый Web-сервер запущен на http://%s:%d%s (API: %s)",
                self.host,
                self.port,
                self.ui_path,
                self.api_prefix,
            )
            return True
        except OSError as e:
            logger.error(
                "Не удалось запустить Web-сервер на %s:%d: %s",
                self.host,
                self.port,
                e,
            )
            return False

    async def stop(self) -> None:
        """Останавливает HTTP-сервер."""
        if not self._is_running:
            return

        logger.info("Остановка Web-сервера...")
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            self._app = None

        self._is_running = False
