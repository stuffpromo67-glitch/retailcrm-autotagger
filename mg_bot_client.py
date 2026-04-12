"""
mg_bot_client.py — MG Bot API client for RetailCRM
"""

import logging
import httpx

logger = logging.getLogger(__name__)
_API = "/api/bot/v1"


class MGBotClient:
    def __init__(self, endpoint, token):
        self.endpoint = endpoint.rstrip("/")
        self.token = token
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

    async def get_chat(self, chat_id):
        resp = await self._http.get(f"{_API}/chats/{chat_id}")
        resp.raise_for_status()
        return resp.json()

    async def set_chat_tag(self, chat_id, tag):
        try:
            chat = await self.get_chat(chat_id)
            existing = [t["name"] for t in chat.get("tags", [])]
        except Exception:
            existing = []
        if tag not in existing:
            existing.append(tag)
        resp = await self._http.patch(
            f"{_API}/chats/{chat_id}",
            json={"tags": [{"name": t} for t in existing]},
        )
        if resp.is_success:
            logger.info("Tag '%s' set on chat #%d", tag, chat_id)
            return True
        logger.error("Failed to set tag on chat #%d: %s %s", chat_id, resp.status_code, resp.text)
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
