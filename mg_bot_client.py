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
        self._crm_http = httpx.AsyncClient(timeout=30.0)

    @property
    def ws_url(self):
        base = self.endpoint.replace("https://", "wss://").replace("http://", "ws://")
        return f"{base}{_API}/ws?events=message_new"

    async def get_chat_messages(self, chat_id, limit=30):
        resp = await self._http.get(f"{_API}/messages", params={"chat_id": chat_id, "limit": limit})
        resp.raise_for_status()
        return resp.json()

    async def find_crm_customer_by_mg_id(self, mg_customer_id):
        """Find RetailCRM customer ID by MG Bot customer ID."""
        if not self.retailcrm_url or not self.retailcrm_api_key:
            return None
        url = f"{self.retailcrm_url}/api/v5/customers"
        params = {
            "apiKey": self.retailcrm_api_key,
            "filter[mgCustomerId]": mg_customer_id,
            "limit": 20,
        }
        try:
            resp = await self._crm_http.get(url, params=params)
            if resp.is_success:
                data = resp.json()
                customers = data.get("customers", [])
                if customers:
                    return customers[0].get("id")
            logger.warning("Customer not found for mgCustomerId=%s", mg_customer_id)
        except Exception as exc:
            logger.error("Error searching customer: %s", exc)
        return None

    async def set_customer_tag(self, crm_customer_id, tag):
        """Set tag on customer via RetailCRM API v5."""
        if not self.retailcrm_url or not self.retailcrm_api_key:
            logger.error("RetailCRM URL or API key not configured")
            return False
        url = f"{self.retailcrm_url}/api/v5/customers/{crm_customer_id}/edit"
        import json
        data = {
            "apiKey": self.retailcrm_api_key,
            "by": "id",
            "customer": json.dumps({"addTags": [tag]}),
        }
        try:
            resp = await self._crm_http.post(url, data=data)
            if resp.is_success:
                result = resp.json()
                if result.get("success"):
                    logger.info("Tag '%s' set on CRM customer %s", tag, crm_customer_id)
                    return True
            logger.error("Failed to set tag: %s %s", resp.status_code, resp.text[:200])
            return False
        except Exception as exc:
            logger.error("Error setting tag: %s", exc)
            return False

    async def close(self):
        await self._http.aclose()
        await self._crm_http.aclose()


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
