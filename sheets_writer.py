"""
sheets_writer.py — Google Sheets writer for quality reports
"""

import json
import logging
import time
from collections import defaultdict

import httpx
import jwt

logger = logging.getLogger(__name__)


class GoogleSheetsWriter:
    def __init__(self, credentials_json, spreadsheet_id):
        self.spreadsheet_id = spreadsheet_id
        self._creds = json.loads(credentials_json) if isinstance(credentials_json, str) else credentials_json
        self._token = None
        self._token_exp = 0

    async def _get_token(self):
        now = time.time()
        if self._token and now < self._token_exp - 60:
            return self._token
        payload = {
            "iss": self._creds["client_email"],
            "scope": "https://www.googleapis.com/auth/spreadsheets",
            "aud": self._creds["token_uri"],
            "iat": int(now),
            "exp": int(now) + 3600,
        }
        signed = jwt.encode(payload, self._creds["private_key"], algorithm="RS256")
        async with httpx.AsyncClient() as c:
            r = await c.post(self._creds["token_uri"], data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": signed,
            })
            r.raise_for_status()
            d = r.json()
            self._token = d["access_token"]
            self._token_exp = now + d.get("expires_in", 3600)
            return self._token

    async def _append(self, rows):
        token = await self._get_token()
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}/values/Лист1!A1:append"
        async with httpx.AsyncClient() as c:
            r = await c.post(url, headers={"Authorization": f"Bearer {token}"},
                             params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
                             json={"values": rows})
            if r.is_success:
                logger.info("Appended %d rows to Google Sheet", len(rows))
                return True
            logger.error("Sheet append failed: %s %s", r.status_code, r.text[:200])
            return False

    async def _is_empty(self):
        token = await self._get_token()
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}/values/Лист1!A1:A1"
        async with httpx.AsyncClient() as c:
            r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
            if r.is_success:
                return not r.json().get("values", [])
        return True

    async def write_report(self, rows, date_str):
        if not rows:
            return
        data = []
        if await self._is_empty():
            data.append(["Дата", "Менеджер", "Клиент", "ID диалога",
                         "Скорость ответа (мин)", "Вежливость (1-10)",
                         "Альтернатива", "Допродажа", "Все вопросы",
                         "Итог", "Оценка (1-10)", "Комментарий"])
        for r in rows:
            data.append([
                r.get("date", date_str), r.get("manager", ""), r.get("customer", ""),
                str(r.get("dialog_id", "")), str(r.get("response_time_min") or "N/A"),
                str(r.get("politeness", "")), r.get("offered_alternative", ""),
                r.get("upsell_attempt", ""), r.get("answered_all_questions", ""),
                r.get("outcome", ""), str(r.get("overall_score", "")), r.get("comment", ""),
            ])
        # Summary
        ms = defaultdict(list)
        for r in rows:
            if r.get("overall_score"):
                ms[r["manager"]].append(r["overall_score"])
        data.append([])
        data.append([f"СВОДКА {date_str}", "", "", "", "", "", "", "", "", "", "", ""])
        data.append(["Менеджер", "Диалогов", "Средний балл", "", "", "", "", "", "", "", "", ""])
        for m, scores in sorted(ms.items()):
            data.append([m, str(len(scores)), str(round(sum(scores)/len(scores), 1)),
                         "", "", "", "", "", "", "", "", ""])
        data.append([])
        await self._append(data)
