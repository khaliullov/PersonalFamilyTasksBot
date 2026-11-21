import asyncio
from datetime import datetime
import json
import logging
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("FamilyTasksBot.TrelloClient")

TRELLO_API_BASE = "https://api.trello.com/1"


class TrelloClient:
    """
    Асинхронный клиент для взаимодействия с Trello REST API.
    """

    def __init__(
        self,
        api_key: str,
        token: str,
        base_url: str = TRELLO_API_BASE,
        timeout_seconds: int = 20,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.api_key = api_key.strip()
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._external_session = session
        self._session: Optional[aiohttp.ClientSession] = session

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    def _auth_params(self, extra_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "key": self.api_key,
            "token": self.token,
        }
        if extra_params:
            for k, v in extra_params.items():
                if v is not None:
                    params[k] = v
        return params

    async def get_board_lists(self, board_id: str) -> List[Dict[str, Any]]:
        """
        Получает список открытых колонок (lists) на доске Trello.
        """
        url = f"{self.base_url}/boards/{board_id}/lists"
        params = self._auth_params({"filter": "open"})
        session = await self._get_session()

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error("Ошибка при получении списков доски %s: HTTP %d: %s", board_id, resp.status, text)
                    return []
                data = await resp.json()
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.error("Сетевая ошибка при запросе списков доски %s: %s", board_id, e)
            return []

    async def get_cards_in_list(self, list_id: str) -> List[Dict[str, Any]]:
        """
        Получает карточки из указанной колонки (list) Trello.
        """
        url = f"{self.base_url}/lists/{list_id}/cards"
        params = self._auth_params({"filter": "open"})
        session = await self._get_session()

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error("Ошибка при получении карточек списка %s: HTTP %d: %s", list_id, resp.status, text)
                    return []
                data = await resp.json()
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.error("Сетевая ошибка при запросе карточек списка %s: %s", list_id, e)
            return []

    async def get_cards_on_board(self, board_id: str) -> List[Dict[str, Any]]:
        """
        Возвращает все открытые карточки на указанной доске Trello.
        """
        url = f"{self.base_url}/boards/{board_id}/cards"
        params = self._auth_params({"filter": "open"})
        session = await self._get_session()
        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error("Ошибка при получении карточек доски %s: HTTP %d: %s", board_id, resp.status, text)
                    return []
                data = await resp.json()
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.error("Сетевая ошибка при получении карточек доски Trello %s: %s", board_id, e)
            return []

    async def get_card(self, card_id: str) -> Optional[Dict[str, Any]]:
        """
        Получает информацию о конкретной карточке по её ID.
        """
        url = f"{self.base_url}/cards/{card_id}"
        params = self._auth_params()
        session = await self._get_session()

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning("Карточка %s не найдена или ошибка HTTP %d: %s", card_id, resp.status, text)
                    return None
                return await resp.json()
        except Exception as e:
            logger.error("Ошибка при получении карточки %s: %s", card_id, e)
            return None

    async def create_card(
        self,
        list_id: str,
        title: str,
        description: Optional[str] = None,
        due: Optional[datetime] = None,
        pos: str = "top",
    ) -> Optional[Dict[str, Any]]:
        """
        Создает новую карточку в указанной колонке Trello.
        Возвращает словарь с данными созданной карточки (включая 'id' и 'url').
        """
        url = f"{self.base_url}/cards"
        params = self._auth_params()
        payload: Dict[str, Any] = {
            "idList": list_id,
            "name": title,
            "pos": pos,
        }
        if description:
            payload["desc"] = description
        if due:
            # Trello ожидает время в формате ISO 8601 (UTC)
            payload["due"] = due.isoformat()

        session = await self._get_session()
        try:
            async with session.post(url, params=params, json=payload) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    logger.error("Не удалось создать карточку в Trello (список %s): HTTP %d: %s", list_id, resp.status, text)
                    return None
                data = await resp.json()
                logger.info("Карточка Trello успешно создана: ID=%s ('%s')", data.get("id"), title)
                return data
        except Exception as e:
            logger.error("Сетевая ошибка при создании карточки в Trello: %s", e)
            return None

    async def update_card(
        self,
        card_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        due: Optional[datetime] = None,
        list_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Обновляет поля существующей карточки Trello.
        """
        url = f"{self.base_url}/cards/{card_id}"
        params = self._auth_params()
        payload: Dict[str, Any] = {}

        if title is not None:
            payload["name"] = title
        if description is not None:
            payload["desc"] = description
        if due is not None:
            payload["due"] = due.isoformat()
        if list_id is not None:
            payload["idList"] = list_id

        if not payload:
            return None

        session = await self._get_session()
        try:
            async with session.put(url, params=params, json=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error("Не удалось обновить карточку Trello %s: HTTP %d: %s", card_id, resp.status, text)
                    return None
                return await resp.json()
        except Exception as e:
            logger.error("Сетевая ошибка при обновлении карточки Trello %s: %s", card_id, e)
            return None

    async def list_webhooks(self) -> List[Dict[str, Any]]:
        """
        Возвращает список всех зарегистрированных вебхуков для текущего токена.
        """
        url = f"{self.base_url}/tokens/{self.token}/webhooks"
        params = {"key": self.api_key}
        session = await self._get_session()

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning("Не удалось получить список вебхуков: HTTP %d: %s", resp.status, text)
                    return []
                data = await resp.json()
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.error("Ошибка при получении списка вебхуков Trello: %s", e)
            return []

    async def register_webhook(
        self,
        callback_url: str,
        id_model: str,
        description: str = "Trello Task Bot Webhook",
    ) -> Optional[Dict[str, Any]]:
        """
        Регистрирует новый вебхук в Trello на доску (id_model).
        Trello отправит предварительный запрос HEAD на callback_url для проверки доступности.
        """
        url = f"{self.base_url}/tokens/{self.token}/webhooks"
        params = {"key": self.api_key}
        payload = {
            "callbackURL": callback_url,
            "idModel": id_model,
            "description": description,
            "active": True,
        }

        session = await self._get_session()
        try:
            async with session.post(url, params=params, json=payload) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    logger.warning(
                        "Не удалось зарегистрировать вебхук Trello (%s -> %s): HTTP %d: %s",
                        id_model,
                        callback_url,
                        resp.status,
                        text,
                    )
                    return None
                data = await resp.json()
                logger.info("Вебхук Trello успешно зарегистрирован (ID %s)", data.get("id"))
                return data
        except Exception as e:
            logger.error("Ошибка при регистрации вебхука Trello: %s", e)
            return None

    async def delete_webhook(self, webhook_id: str) -> bool:
        """
        Удаляет вебхук по его ID.
        """
        url = f"{self.base_url}/webhooks/{webhook_id}"
        params = self._auth_params()
        session = await self._get_session()

        try:
            async with session.delete(url, params=params) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error("Ошибка при удалении вебхука Trello %s: %s", webhook_id, e)
            return False

    async def close(self) -> None:
        """Закрывает клиентскую сессию HTTP, если она была создана локально."""
        if self._session and not self._session.closed and self._external_session is None:
            await self._session.close()
            self._session = None
