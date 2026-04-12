"""
retailcrm_client.py — Клиент RetailCRM API v5
Получает диалоги и проставляет теги через REST API.
"""

import logging
import requests

logger = logging.getLogger(__name__)


class RetailCRMClient:
    """
    Обёртка над RetailCRM API v5.

    Документация API: https://docs.retailcrm.ru/Developers/API/APIVersions/APIv5
    """

    def __init__(self, base_url: str, api_key: str):
        """
        Args:
            base_url: URL вашего аккаунта, например https://myshop.retailcrm.ru
            api_key:  API-ключ из раздела «Администрирование → API-доступ»
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"X-API-KEY": api_key})

    # ------------------------------------------------------------------
    # Работа с диалогами (Conversations)
    # ------------------------------------------------------------------

    def get_conversation(self, conversation_id: int) -> dict:
        """Получить данные одного диалога по ID."""
        url = f"{self.base_url}/api/v5/conversations/{conversation_id}"
        resp = self.session.get(url, params={"apiKey": self.api_key})
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"RetailCRM error: {data}")
        return data.get("conversation", {})

    def get_conversation_messages(self, conversation_id: int, limit: int = 20) -> list[dict]:
        """
        Получить последние сообщения диалога.
        Возвращает список сообщений в хронологическом порядке.
        """
        url = f"{self.base_url}/api/v5/conversations/{conversation_id}/messages"
        resp = self.session.get(
            url,
            params={"apiKey": self.api_key, "limit": limit, "page": 1},
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            logger.warning("Ошибка при получении сообщений: %s", data)
            return []
        return data.get("messages", [])

    def add_tag_to_conversation(self, conversation_id: int, tag: str) -> bool:
        """
        Добавить тег к диалогу.

        RetailCRM API v5 принимает теги в виде POST-запроса:
        POST /api/v5/conversations/{id}/tag
        """
        url = f"{self.base_url}/api/v5/conversations/{conversation_id}/tag"
        resp = self.session.post(
            url,
            data={"apiKey": self.api_key, "tag": tag},
        )
        if resp.status_code == 200 and resp.json().get("success"):
            logger.info("Тег «%s» добавлен к диалогу #%d", tag, conversation_id)
            return True

        # Запасной вариант: обновить диалог через PUT с полем tags[]
        return self._update_conversation_tags(conversation_id, tag)

    def _update_conversation_tags(self, conversation_id: int, tag: str) -> bool:
        """
        Альтернативный способ простановки тега — обновление диалога через PUT.
        Используется если POST /tag не поддерживается в вашей версии RetailCRM.
        """
        # Сначала получаем текущие теги, чтобы не затереть существующие
        try:
            conv = self.get_conversation(conversation_id)
            existing_tags = [t["name"] for t in conv.get("tags", [])]
        except Exception:
            existing_tags = []

        if tag not in existing_tags:
            existing_tags.append(tag)

        url = f"{self.base_url}/api/v5/conversations/{conversation_id}/edit"
        payload = {
            "apiKey": self.api_key,
            "conversation[tags]": existing_tags,
        }
        resp = self.session.post(url, data=payload)

        if resp.status_code == 200 and resp.json().get("success"):
            logger.info("Тег «%s» обновлён у диалога #%d (через edit)", tag, conversation_id)
            return True

        logger.error(
            "Не удалось поставить тег диалогу #%d: %s %s",
            conversation_id,
            resp.status_code,
            resp.text,
        )
        return False

    # ------------------------------------------------------------------
    # Работа с заказами (если диалог привязан к заказу)
    # ------------------------------------------------------------------

    def add_tag_to_order(self, order_id: int, tag: str) -> bool:
        """
        Добавить тег к заказу, связанному с диалогом.
        Используется как дополнительный вариант, если теги хранятся на заказе.
        """
        # Получаем текущие теги заказа
        url = f"{self.base_url}/api/v5/orders/{order_id}"
        resp = self.session.get(url, params={"apiKey": self.api_key})
        resp.raise_for_status()
        order_data = resp.json().get("order", {})
        existing_tags = [t["name"] for t in order_data.get("tags", [])]

        if tag not in existing_tags:
            existing_tags.append(tag)

        # Обновляем заказ с новыми тегами
        edit_url = f"{self.base_url}/api/v5/orders/{order_id}/edit"
        payload = {"apiKey": self.api_key}
        for i, t in enumerate(existing_tags):
            payload[f"order[tags][{i}][name]"] = t

        resp = self.session.post(edit_url, data=payload)
        if resp.status_code == 200 and resp.json().get("success"):
            logger.info("Тег «%s» добавлен к заказу #%d", tag, order_id)
            return True

        logger.error("Ошибка при обновлении тегов заказа #%d: %s", order_id, resp.text)
        return False


def build_dialog_text(messages: list[dict]) -> str:
    """
    Формирует текстовое представление диалога из списка сообщений RetailCRM.

    Пример структуры одного сообщения:
    {
        "id": 1,
        "type": "message",
        "from": {"type": "customer", "name": "Иван"},
        "body": "Хочу вернуть товар"
    }
    """
    lines = []
    for msg in messages:
        sender_info = msg.get("from", {})
        sender_type = sender_info.get("type", "unknown")
        sender_name = sender_info.get("name", "")

        if sender_type == "customer":
            role = f"Клиент ({sender_name})" if sender_name else "Клиент"
        elif sender_type in ("user", "manager", "operator"):
            role = f"Менеджер ({sender_name})" if sender_name else "Менеджер"
        else:
            role = sender_type.capitalize()

        body = msg.get("body") or msg.get("text") or ""
        if body:
            lines.append(f"{role}: {body}")

    return "\n".join(lines)
