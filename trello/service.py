import asyncio
from datetime import datetime
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from trello.client import TrelloClient
from trello.config import (
    TRELLO_API_KEY,
    TRELLO_API_SECRET,
    TRELLO_BOARD_ID,
    TRELLO_DONE_LISTS,
    TRELLO_ENABLED,
    TRELLO_INBOX_LISTS,
    TRELLO_POLL_INTERVAL_SECONDS,
    TRELLO_TOKEN,
    TRELLO_WEBHOOK_CALLBACK_URL,
    TRELLO_WEBHOOK_ENABLED,
    TRELLO_WEBHOOK_HOST,
    TRELLO_WEBHOOK_PATH,
    TRELLO_WEBHOOK_PORT,
)
from trello.matcher import (
    find_all_matching_lists,
    find_matching_list,
    is_list_name_matching,
)
from trello.webhook import TrelloWebhookServer

logger = logging.getLogger("FamilyTasksBot.TrelloService")


class TrelloSyncService:
    """
    Основной координатор синхронизации задач с Trello:
    1. Автоматический поиск и привязка колонок 'Inbox' / 'Входящие' и 'Done' / 'Завершено'.
    2. Создание карточек в колонке Inbox при появлении новых задач.
    3. Обработка входящих вебхуков (callbacks) при переносе карточек в колонку завершенных.
    4. Систематическое периодическое сканирование (fallback polling) на случай отсутствия или сбоя вебхука.
    """

    def __init__(
        self,
        api_key: str = TRELLO_API_KEY,
        api_secret: str = TRELLO_API_SECRET,
        token: str = TRELLO_TOKEN,
        board_id: str = TRELLO_BOARD_ID,
        inbox_candidates: Optional[List[str]] = None,
        done_candidates: Optional[List[str]] = None,
        poll_interval_seconds: int = TRELLO_POLL_INTERVAL_SECONDS,
        webhook_enabled: bool = TRELLO_WEBHOOK_ENABLED,
        webhook_host: str = TRELLO_WEBHOOK_HOST,
        webhook_port: int = TRELLO_WEBHOOK_PORT,
        webhook_path: str = TRELLO_WEBHOOK_PATH,
        webhook_callback_url: Optional[str] = TRELLO_WEBHOOK_CALLBACK_URL,
        client: Optional[TrelloClient] = None,
    ):
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.token = token.strip()
        self.board_id = board_id.strip()

        self.inbox_candidates = inbox_candidates or list(TRELLO_INBOX_LISTS)
        self.done_candidates = done_candidates or list(TRELLO_DONE_LISTS)
        self.poll_interval = poll_interval_seconds

        self.webhook_enabled = webhook_enabled
        self.webhook_host = webhook_host
        self.webhook_port = webhook_port
        self.webhook_path = webhook_path
        self.webhook_callback_url = webhook_callback_url

        self.client = client or TrelloClient(api_key=self.api_key, token=self.token)

        self.inbox_list_id: Optional[str] = None
        self.inbox_list_name: Optional[str] = None
        self.done_list_ids: Set[str] = set()
        self.done_list_names: List[str] = []

        self._on_card_completed_callback: Optional[
            Callable[[str, str], Coroutine[Any, Any, None]]
        ] = None
        self._on_card_updated_callback: Optional[
            Callable[[str, str, Optional[str], Optional[str]], Coroutine[Any, Any, None]]
        ] = None
        self._on_cards_created_callback: Optional[
            Callable[[List[Dict[str, Any]]], Coroutine[Any, Any, None]]
        ] = None
        self._card_lookup_fn: Optional[
            Callable[[str], Coroutine[Any, Any, Optional[Dict[str, Any]]]]
        ] = None

        self._webhook_server: Optional[Any] = None
        self._owns_webhook_server = False
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._is_running = False

    @property
    def is_enabled(self) -> bool:
        """Интеграция активна только если включена и заданы ключи и ID доски."""
        return bool(
            TRELLO_ENABLED
            and self.api_key
            and self.token
            and self.board_id
        )

    async def resolve_board_lists(self) -> bool:
        """
        Запрашивает все открытые колонки на доске Trello и находит:
        - Колонку Inbox для новых задач.
        - Колонки Done для выполненных задач.
        """
        if not self.board_id:
            logger.warning("TRELLO_BOARD_ID не настроен. Поиск колонок невозможен.")
            return False

        logger.info("Запрос списка колонок с доски Trello (ID: %s)...", self.board_id)
        lists = await self.client.get_board_lists(self.board_id)
        if not lists:
            logger.error("Не удалось получить колонки с доски Trello (список пуст или ошибка API).")
            return False

        list_summary = ", ".join(f"'{lst.get('name')}'" for lst in lists)
        logger.info("Доступные колонки на доске Trello: [%s]", list_summary)

        # 1. Поиск колонки Inbox / Входящие
        matched_inbox = find_matching_list(lists, self.inbox_candidates)
        if matched_inbox:
            self.inbox_list_id = matched_inbox["id"]
            self.inbox_list_name = matched_inbox["name"]
            logger.info(
                "✅ Найдена колонка для входящих задач: '%s' (ID: %s)",
                self.inbox_list_name,
                self.inbox_list_id,
            )
        else:
            logger.warning(
                "⚠️ Не найдена колонка для входящих задач среди кандидатов: %s. "
                "Использована первая колонка доски: '%s'",
                self.inbox_candidates,
                lists[0]["name"],
            )
            self.inbox_list_id = lists[0]["id"]
            self.inbox_list_name = lists[0]["name"]

        # 2. Поиск колонок Done / Завершено
        matched_done = find_all_matching_lists(lists, self.done_candidates)
        self.done_list_ids.clear()
        self.done_list_names.clear()

        for d_list in matched_done:
            self.done_list_ids.add(d_list["id"])
            self.done_list_names.append(d_list["name"])

        if self.done_list_ids:
            logger.info(
                "✅ Найдены колонки завершенных задач: %s (IDs: %s)",
                self.done_list_names,
                list(self.done_list_ids),
            )
        else:
            logger.warning(
                "⚠️ Не найдены колонки завершенных задач среди кандидатов: %s",
                self.done_candidates,
            )

        return bool(self.inbox_list_id)

    async def sync_task_created(
        self,
        task_id: int,
        title: str,
        description: Optional[str] = None,
        deadline_dt: Optional[datetime] = None,
    ) -> Optional[Dict[str, str]]:
        """
        Создает карточку в колонке Inbox Trello для только что созданной задачи.
        Возвращает {'card_id': ..., 'card_url': ...} или None при ошибке.
        """
        if not self.is_enabled:
            return None

        if not self.inbox_list_id:
            resolved = await self.resolve_board_lists()
            if not resolved or not self.inbox_list_id:
                logger.error("Невозможно создать карточку Trello: целевая колонка не определена.")
                return None

        # Формируем аккуратное описание карточки с привязкой к ID в SQLite
        card_title = title
        desc_parts = [
            f"📌 **Задача #{task_id}** (создана ботом из Telegram)",
        ]
        if deadline_dt:
            dl_str = deadline_dt.strftime("%Y-%m-%d %H:%M:%S")
            desc_parts.append(f"⏰ **Дедлайн:** {dl_str}")
        if description:
            desc_parts.append(f"\n{description}")

        card_desc = "\n".join(desc_parts)

        card = await self.client.create_card(
            list_id=self.inbox_list_id,
            title=card_title,
            description=card_desc,
            due=deadline_dt,
            pos="top",
        )

        if not card:
            return None

        card_id = card.get("id")
        card_url = card.get("shortUrl") or card.get("url") or f"https://trello.com/c/{card.get('shortLink')}"
        logger.info(
            "Карточка Trello для задачи #%d создана: ID=%s URL=%s",
            task_id,
            card_id,
            card_url,
        )
        return {
            "card_id": card_id,
            "card_url": card_url,
        }

    async def sync_task_updated(
        self,
        task_id: int,
        trello_card_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        deadline_dt: Optional[datetime] = None,
    ) -> bool:
        """
        Синхронизирует обновления задачи в карточку Trello.
        """
        if not self.is_enabled or not trello_card_id:
            return False

        desc_parts = [
            f"📌 **Задача #{task_id}** (обновлена)",
        ]
        if deadline_dt:
            dl_str = deadline_dt.strftime("%Y-%m-%d %H:%M:%S")
            desc_parts.append(f"⏰ **Дедлайн:** {dl_str}")
        if description:
            desc_parts.append(f"\n{description}")
        full_desc = "\n".join(desc_parts)

        res = await self.client.update_card(
            card_id=trello_card_id,
            title=title,
            description=full_desc,
            due=deadline_dt,
        )
        return res is not None

    async def attach_image_to_card(
        self,
        card_id: str,
        file_path: str,
        *,
        filename: Optional[str] = None,
        mime_type: str = "image/jpeg",
        name: Optional[str] = None,
        max_attempts: int = 3,
        base_delay_seconds: float = 2.0,
    ) -> Optional[Dict[str, Any]]:
        """
        Прикрепляет оригинальное изображение к карточке Trello с проверкой дубликатов
        и ограниченными повторами (exponential backoff) при транзиентных сбоях.
        """
        if not self.is_enabled or not card_id or not file_path:
            return None

        target_name = name or filename or "photo.jpg"

        # Проверка существующих вложений по имени файла для предотвращения дубликатов при ретраях
        if hasattr(self.client, "get_card_attachments"):
            try:
                existing = await self.client.get_card_attachments(card_id)
                for att in existing:
                    if isinstance(att, dict) and att.get("name") == target_name:
                        logger.info(
                            "Вложение '%s' уже присутствует на карточке Trello %s (id=%s).",
                            target_name,
                            card_id,
                            att.get("id"),
                        )
                        return att
            except Exception as exc:
                logger.debug("Проверка существующих вложений карточки %s пропущена: %s", card_id, type(exc).__name__)

        attempts = max(1, int(max_attempts))
        for attempt in range(1, attempts + 1):
            result = await self.client.upload_card_attachment(
                card_id=card_id,
                file_path=file_path,
                filename=filename,
                mime_type=mime_type,
                name=target_name,
            )
            if result is not None:
                return result

            if attempt < attempts:
                delay = base_delay_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "Повтор загрузки вложения на карточку Trello %s через %.1f с (попытка %d/%d)...",
                    card_id,
                    delay,
                    attempt,
                    attempts,
                )
                if delay > 0:
                    await asyncio.sleep(delay)

        return None

    async def sync_task_completed(self, trello_card_id: str) -> bool:
        """
        Перемещает карточку в колонку Done (завершенные) в Trello.
        """
        if not self.is_enabled or not trello_card_id:
            return False

        if not self.done_list_ids:
            await self.resolve_board_lists()

        if not self.done_list_ids:
            logger.warning("Не найдены колонки Done для перемещения карточки %s", trello_card_id)
            return False

        done_list_id = next(iter(self.done_list_ids))
        res = await self.client.update_card(
            card_id=trello_card_id,
            list_id=done_list_id,
        )
        return res is not None

    def is_done_list(self, list_id: Optional[str], list_name: Optional[str] = None) -> bool:
        """
        Проверяет, является ли колонка колонкой завершенных задач
        (по совпадению ID или имени с кандидатами).
        """
        if list_id and list_id in self.done_list_ids:
            return True
        if list_name and is_list_name_matching(list_name, self.done_candidates):
            return True
        return False

    async def handle_webhook_payload(self, payload: Dict[str, Any]) -> None:
        """
        Обрабатывает событие вебхука от Trello:
        Если карточка перемещена или создана в одной из колонок 'Done' / 'Завершено',
        вызывает колбэк завершения задачи.
        """
        action = payload.get("action", {})
        action_type = action.get("type")
        data = action.get("data", {})

        card_info = data.get("card", {})
        card_id = card_info.get("id")
        card_name = card_info.get("name", "")

        if not card_id:
            return

        is_completed = False

        # 1. Перемещение карточки между колонками (updateCard)
        if action_type == "updateCard":
            list_after = data.get("listAfter")
            if list_after:
                dest_id = list_after.get("id")
                dest_name = list_after.get("name", "")
                if self.is_done_list(dest_id, dest_name):
                    logger.info(
                        "Вебхук Trello: карточка '%s' (%s) перемещена в колонку завершенных '%s'",
                        card_name,
                        card_id,
                        dest_name,
                    )
                    is_completed = True

        # 2. Создание карточки непосредственно в Done (createCard, copyCard, etc.)
        elif action_type in ("createCard", "copyCard", "emailCard", "moveCardToBoard"):
            list_data = data.get("list", {})
            dest_id = list_data.get("id")
            dest_name = list_data.get("name", "")
            if self.is_done_list(dest_id, dest_name):
                logger.info(
                    "Вебхук Trello: карточка '%s' (%s) создана в колонке завершенных '%s'",
                    card_name,
                    card_id,
                    dest_name,
                )
                is_completed = True
            else:
                logger.info(
                    "Вебхук Trello: карточка '%s' (%s) создана в колонке '%s'",
                    card_name,
                    card_id,
                    dest_name,
                )
                if self._on_cards_created_callback and card_id:
                    # Запрашиваем полные данные карточки из Trello API (desc, due, shortUrl)
                    full_card = None
                    try:
                        full_card = await self.client.get_card(card_id)
                    except Exception as e:
                        logger.debug("Не удалось запросить полные данные карточки %s: %s", card_id, e)
                    if not full_card:
                        full_card = {
                            "id": card_id,
                            "name": card_name,
                            "desc": card_info.get("desc", ""),
                            "due": card_info.get("due"),
                            "shortUrl": card_info.get("shortUrl") or f"https://trello.com/c/{card_info.get('shortLink') or card_id}",
                        }
                    await self._on_cards_created_callback([full_card])
                    return

        if is_completed and self._on_card_completed_callback:
            await self._on_card_completed_callback(card_id, card_name)
        elif not is_completed and self._on_card_updated_callback and card_id and card_name:
            # Обработка обновления карточки (переименование, изменение описания или дедлайна)
            old_data = data.get("old", {})
            if "name" in old_data or "desc" in old_data or "due" in old_data or card_name:
                await self._on_card_updated_callback(
                    card_id,
                    card_name,
                    card_info.get("desc"),
                    card_info.get("due"),
                )

    async def poll_done_columns(self) -> None:
        """
        Периодическое систематическое сканирование (fallback polling):
        Опрашивает колонки завершенных задач на доске Trello и для каждой
        карточки вызывает колбэк завершения.
        """
        if not self.is_enabled:
            return

        if not self.done_list_ids:
            await self.resolve_board_lists()

        if not self.done_list_ids:
            logger.debug("Систематическое сканирование: нет активных колонок Done.")
            return

        logger.debug(
            "Систематическое сканирование колонок Done Trello: %s...",
            list(self.done_list_ids),
        )

        for done_id in list(self.done_list_ids):
            cards = await self.client.get_cards_in_list(done_id)
            for card in cards:
                card_id = card.get("id")
                card_name = card.get("name", "")
                if card_id and self._on_card_completed_callback:
                    await self._on_card_completed_callback(card_id, card_name)

    async def poll_active_and_new_cards(self) -> List[Dict[str, Any]]:
        """
        Опрашивает все открытые карточки на доске:
        - Карточки, которых еще нет в БД, передаются в _on_cards_created_callback (импорт + TG).
        - Карточки, которые уже есть в БД, синхронизируются через _on_card_updated_callback.
        """
        if not self.is_enabled:
            return []

        if not hasattr(self.client, "get_cards_on_board"):
            return []

        cards = await self.client.get_cards_on_board(self.board_id)
        if not cards:
            return []

        new_cards: List[Dict[str, Any]] = []

        for card in cards:
            card_id = card.get("id")
            card_name = card.get("name", "")
            list_id = card.get("idList")

            if not card_id or not card_name:
                continue

            # Карточки в Done обрабатываются через poll_done_columns
            if self.is_done_list(list_id):
                continue

            # Проверяем, известна ли карточка в БД
            is_known = False
            if self._card_lookup_fn:
                try:
                    existing_task = await self._card_lookup_fn(card_id)
                    if existing_task:
                        is_known = True
                except Exception as e:
                    logger.debug("Ошибка в _card_lookup_fn для %s: %s", card_id, e)
            else:
                try:
                    from database import get_task_by_trello_card_id
                    existing_task = await get_task_by_trello_card_id(card_id)
                    if existing_task:
                        is_known = True
                except Exception as e:
                    logger.debug("Ошибка при проверке наличия карточки %s в БД: %s", card_id, e)

            if is_known or not self._on_cards_created_callback:
                if self._on_card_updated_callback:
                    await self._on_card_updated_callback(
                        card_id,
                        card_name,
                        card.get("desc"),
                        card.get("due"),
                    )
            else:
                # Проверяем описание на предмет задачи бота "📌 **Задача #..."
                card_desc = card.get("desc") or ""
                if "Задача #" in card_desc:
                    try:
                        from database import find_active_task_for_trello_card
                        found_t = await find_active_task_for_trello_card(card_id=card_id, card_title=card_name)
                        if found_t:
                            is_known = True
                    except Exception:
                        pass
                    if is_known:
                        if self._on_card_updated_callback:
                            await self._on_card_updated_callback(
                                card_id,
                                card_name,
                                card.get("desc"),
                                card.get("due"),
                            )
                        continue
                new_cards.append(card)

        if new_cards and self._on_cards_created_callback:
            await self._on_cards_created_callback(new_cards)

        return new_cards

    async def poll_active_cards(self) -> None:
        """
        Опрашивает все открытые карточки на доске и синхронизирует изменения
        (название, описание, дедлайн) для активных задач из любых колонок доски.
        """
        await self.poll_active_and_new_cards()

    async def poll_all_and_sync(self) -> List[Dict[str, Any]]:
        """
        Комплексный опрос доски: синхронизация завершенных задач,
        импорт новых задач из Trello в базу и обновление активных карточек.
        """
        await self.poll_done_columns()
        return await self.poll_active_and_new_cards()

    async def _setup_trello_webhook_if_configured(self) -> None:
        """
        Если задан публичный TRELLO_WEBHOOK_CALLBACK_URL, проверяет и при необходимости
        автоматически регистрирует вебхук на доску в Trello.
        """
        if not self.webhook_callback_url:
            logger.info(
                "TRELLO_WEBHOOK_CALLBACK_URL не задан. Вебхук в Trello не регистрируется. "
                "Используется систематическое сканирование (polling fallback)."
            )
            return

        logger.info(
            "Проверка регистрации вебхука Trello на URL: %s",
            self.webhook_callback_url,
        )

        existing_hooks = await self.client.list_webhooks()
        normalized_target = self.webhook_callback_url.rstrip("/")

        for hook in existing_hooks:
            hook_url = hook.get("callbackURL", "").rstrip("/")
            hook_model = hook.get("idModel")
            if hook_url == normalized_target and hook_model == self.board_id:
                logger.info(
                    "Вебхук Trello уже зарегистрирован (ID %s, активен=%s)",
                    hook.get("id"),
                    hook.get("active"),
                )
                return

        # Если не найден — регистрируем
        res = await self.client.register_webhook(
            callback_url=self.webhook_callback_url,
            id_model=self.board_id,
            description="PersonalFamilyTasksBot sync",
        )
        if res:
            logger.info("Вебхук Trello успешно зарегистрирован на %s", self.webhook_callback_url)
        else:
            logger.warning(
                "Не удалось автоматически зарегистрировать вебхук в Trello. "
                "Убедитесь, что сервер доступен по внешнему HTTPS-адресу. "
                "Систематическое сканирование (polling) обеспечит синхронизацию."
            )

    async def start(
        self,
        scheduler: AsyncIOScheduler,
        on_card_completed: Callable[[str, str], Coroutine[Any, Any, None]],
        on_card_updated: Optional[
            Callable[[str, str, Optional[str], Optional[str]], Coroutine[Any, Any, None]]
        ] = None,
        on_cards_created: Optional[
            Callable[[List[Dict[str, Any]]], Coroutine[Any, Any, None]]
        ] = None,
        card_lookup_fn: Optional[
            Callable[[str], Coroutine[Any, Any, Optional[Dict[str, Any]]]]
        ] = None,
        webhook_server: Optional[Any] = None,
    ) -> bool:
        """
        Запускает сервис интеграции с Trello:
        1. Разрешает колонки на доске.
        2. Запускает встроенный вебхук-сервер (или регистрирует в едином web_server).
        3. Настраивает вебхук в Trello (если задан callback URL).
        4. Регистрирует периодическую задачу опроса доски в APScheduler.
        """
        if not self.is_enabled:
            logger.info("Интеграция с Trello отключена или не заданы обязательные параметры в .env.")
            return False

        logger.info("Запуск сервиса синхронизации Trello...")
        self._on_card_completed_callback = on_card_completed
        self._on_card_updated_callback = on_card_updated
        self._on_cards_created_callback = on_cards_created
        self._card_lookup_fn = card_lookup_fn
        self._scheduler = scheduler

        # 1. Разрешаем колонки доски
        await self.resolve_board_lists()

        # 2. Запуск легковесного сервера вебхуков или подключение к единому серверу
        if self.webhook_enabled:
            if webhook_server is not None:
                self._webhook_server = webhook_server
                self._owns_webhook_server = False
                if hasattr(webhook_server, "set_trello_handler"):
                    webhook_server.set_trello_handler(
                        handler=self.handle_webhook_payload,
                        secret=self.api_secret,
                        callback_url=self.webhook_callback_url,
                    )
                logger.info("Вебхуки Trello подключены к единому Web-серверу приложения.")
            else:
                self._webhook_server = TrelloWebhookServer(
                    event_handler=self.handle_webhook_payload,
                    host=self.webhook_host,
                    port=self.webhook_port,
                    path=self.webhook_path,
                    secret=self.api_secret,
                    callback_url=self.webhook_callback_url,
                )
                self._owns_webhook_server = True
                await self._webhook_server.start()

            # 3. Попытка автоматической регистрации вебхука в Trello
            if self.webhook_callback_url:
                await self._setup_trello_webhook_if_configured()

        # 4. Регистрация систематического сканирования (fallback polling) в APScheduler
        poll_job_id = "trello_poll_done_columns"
        self._scheduler.add_job(
            self.poll_all_and_sync,
            trigger="interval",
            seconds=self.poll_interval,
            id=poll_job_id,
            replace_existing=True,
        )
        logger.info(
            "Систематическое сканирование колонок Trello запланировано каждые %d с (job_id='%s').",
            self.poll_interval,
            poll_job_id,
        )

        self._is_running = True
        return True

    async def stop(self) -> None:
        """Останавливает сервис интеграции с Trello."""
        if not self._is_running:
            return

        logger.info("Остановка сервиса TrelloSyncService...")

        # Останавливаем вебхук-сервер (только если владели им)
        if self._webhook_server:
            if getattr(self, "_owns_webhook_server", True) and hasattr(self._webhook_server, "stop"):
                await self._webhook_server.stop()
            self._webhook_server = None

        # Снимаем задачу из планировщика
        if self._scheduler:
            poll_job_id = "trello_poll_done_columns"
            if self._scheduler.get_job(poll_job_id):
                self._scheduler.remove_job(poll_job_id)

        # Закрываем HTTP-сессию
        await self.client.close()

        self._is_running = False
        logger.info("Сервис TrelloSyncService успешно остановлен.")
