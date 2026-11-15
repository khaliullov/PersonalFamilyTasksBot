import asyncio
import base64
import hashlib
import hmac
import json
import logging
from typing import Any, Callable, Coroutine, Dict, Optional

from aiohttp import web

logger = logging.getLogger("FamilyTasksBot.TrelloWebhook")


def verify_trello_webhook_signature(
    request_body: str,
    callback_url: str,
    secret: str,
    header_signature: str,
) -> bool:
    """
    Проверяет криптографическую подпись входящего запроса от Trello (заголовок X-Trello-Webhook).
    Trello вычисляет HMAC-SHA1 от конкатенации сырого тела запроса и зарегистрированного callbackURL,
    используя API Secret приложения, и кодирует результат в base64.
    """
    if not secret or not header_signature:
        return False

    try:
        content = (request_body + callback_url).encode("utf-8")
        secret_bytes = secret.encode("utf-8")
        computed_hmac = hmac.new(secret_bytes, content, hashlib.sha1)
        computed_signature = base64.b64encode(computed_hmac.digest()).decode("utf-8")
        return hmac.compare_digest(computed_signature, header_signature)
    except Exception as e:
        logger.error("Ошибка при проверке HMAC-подписи Trello: %s", e)
        return False


class TrelloWebhookServer:
    """
    Встроенный легковесный HTTP-сервер на aiohttp для приема обратных вызовов (вебхуков) от Trello.
    """

    def __init__(
        self,
        event_handler: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]],
        host: str = "0.0.0.0",
        port: int = 8080,
        path: str = "/trello/webhook",
        secret: Optional[str] = None,
        callback_url: Optional[str] = None,
        enforce_signature: bool = False,
    ):
        self.event_handler = event_handler
        self.host = host
        self.port = port
        self.path = path if path.startswith("/") else f"/{path}"
        self.secret = secret.strip() if secret else None
        self.callback_url = callback_url.strip() if callback_url else None
        self.enforce_signature = enforce_signature

        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._is_running = False

    async def _handle_head(self, request: web.Request) -> web.Response:
        """
        Trello отправляет HEAD-запрос при создании/проверке вебхука.
        Сервер ОБЯЗАН вернуть HTTP 200 OK.
        """
        logger.info("Получен HEAD-запрос проверки вебхука от Trello: %s", request.path)
        return web.Response(status=200)

    async def _handle_get(self, request: web.Request) -> web.Response:
        """Health-check эндпоинт для ручной проверки в браузере или curl."""
        return web.Response(text="Trello Webhook Server is running (OK).", status=200)

    async def _handle_post(self, request: web.Request) -> web.Response:
        """
        Принимает полезную нагрузку от Trello при изменении ресурсов на доске.
        """
        raw_body = await request.text()
        signature_header = request.headers.get("X-Trello-Webhook")

        # Проверка HMAC-подписи при наличии секрета и зарегистрированного URL
        if self.secret and self.callback_url:
            if signature_header:
                is_valid = verify_trello_webhook_signature(
                    request_body=raw_body,
                    callback_url=self.callback_url,
                    secret=self.secret,
                    header_signature=signature_header,
                )
                if not is_valid:
                    logger.warning(
                        "Подпись X-Trello-Webhook не прошла проверку! Header: %s",
                        signature_header,
                    )
                    if self.enforce_signature:
                        return web.Response(text="Invalid signature", status=401)
            else:
                logger.warning("Запрос к Trello webhook получен без заголовка X-Trello-Webhook.")
                if self.enforce_signature:
                    return web.Response(text="Missing signature", status=401)

        try:
            data = json.loads(raw_body)
        except json.JSONDecodeError as e:
            logger.warning("Некорректный JSON в теле вебхука Trello: %s", e)
            return web.Response(text="Invalid JSON", status=400)

        # Вызываем асинхронный обработчик в фоне или напрямую
        try:
            asyncio.create_task(self._safe_dispatch(data))
        except Exception as e:
            logger.error("Ошибка диспетчеризации события вебхука: %s", e)

        # Trello требует быстрый ответ HTTP 200 OK во избежание ретраев
        return web.Response(text="OK", status=200)

    async def _safe_dispatch(self, data: Dict[str, Any]) -> None:
        try:
            await self.event_handler(data)
        except Exception as e:
            logger.exception("Исключение в обработчике события Trello webhook: %s", e)

    async def start(self) -> bool:
        """Запускает HTTP-сервер для приема вебхуков."""
        if self._is_running:
            return True

        self._app = web.Application()
        self._app.router.add_route("HEAD", self.path, self._handle_head)
        self._app.router.add_route("GET", self.path, self._handle_get)
        self._app.router.add_route("POST", self.path, self._handle_post)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)

        try:
            await self._site.start()
            self._is_running = True
            logger.info(
                "HTTP-сервер вебхуков Trello запущен на http://%s:%d%s",
                self.host,
                self.port,
                self.path,
            )
            return True
        except OSError as e:
            logger.error(
                "Не удалось запустить сервер вебхуков Trello на %s:%d: %s. "
                "Будет использоваться polling fallback.",
                self.host,
                self.port,
                e,
            )
            return False

    async def stop(self) -> None:
        """Останавливает сервер вебхуков."""
        if not self._is_running:
            return

        logger.info("Остановка HTTP-сервера вебхуков Trello...")
        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._is_running = False
        logger.info("HTTP-сервер вебхуков Trello остановлен.")
