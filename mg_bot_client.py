"""
mg_bot_client.py — MG Bot API + RetailCRM API client
"""

import logging
import httpx

logger = logging.getLogger(__name__)
_API = "/api/bot/v1"


class MGBotClient:
    def __init__(self, endpoint, token, retailcrm_url=None, retailcrm_api_key=None):
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.retailcrm_url = (retailcrm_url or "").rstrip("/")
        self.retailcrm_api_key = retailcrm_api_key or ""
        self._http = httpx.AsyncClient(
            base_url=self.endpoint,
            headers={"x-bot-token": token, "Content-Type": "application/json"},
            timeout=30.0,
        )

    @property
    def ws_url(self):
        base = self.endpoint.replace("https://", "wss://").replace("http://", "ws://")
        return f"{base}{_API}/ws?events=message_new"

    async def get_chat_messages(self, chat_id, limit=30):
        resp = await self._http.get(f"{_API}/messages", params={"chat_id": chat_id, "limit": limit})
        resp.raise_for_status()
        return resp.json()

    async def get_dialogs(self, chat_id):
        resp = await self._http.get(f"{_API}/dialogs", params={"chat_id": chat_id, "limit": 1})
        resp.raise_for_status()
        dialogs = resp.json()
        if dialogs:
            return dialogs[0]
        return None

    async def add_dialog_tags(self, dialog_id, tags):
        """Add tags to a dialog via MG Bot API.
        tags: list of tag name strings, e.g. ["новый клиент", "ждет ответ"]
        """
        tag_objects = [{"name": t} for t in tags]
        resp = await self._http.patch(
            f"{_API}/dialogs/{dialog_id}/tags/add",
            json={"tags": tag_objects},
        )
        if resp.is_success:
            logger.info("Tags %s added to dialog #%d", tags, dialog_id)
            return True
        logger.error("Failed to add tags to dialog #%d: %s %s", dialog_id, resp.status_code, resp.text[:200])
        return False

    async def close(self):
        await self._http.aclose()


def build_dialog_text(messages):
    lines = []
    for msg in messages:
        from_info = msg.get("from", {})
        from_type = from_info.get("type", "unknown")
        from_name = from_info.get("name", "")
        if from_type == "customer":
            role = f"Client ({from_name})" if from_name else "Client"
        elif from_type in ("user", "manager", "operator", "bot"):
            role = f"Manager ({from_name})" if from_name else "Manager"
        else:
            role = from_type.capitalize()
        content = msg.get("content") or ""
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)
