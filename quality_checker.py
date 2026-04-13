"""
quality_checker.py — Ежедневный контроль качества диалогов менеджеров
Забирает закрытые вчера диалоги, анализирует через Claude, формирует отчёт.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import anthropic
import httpx

logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))

QUALITY_PROMPT = """Ты — контролёр качества обслуживания клиентов интернет-магазина одежды.
Проанализируй диалог менеджера с клиентом и оцени качество работы менеджера.

Верни ответ СТРОГО в формате JSON (без пояснений, без markdown):
{
  "politeness": <число от 1 до 10>,
  "offered_alternative": <true/false — предложил ли альтернативу если нужного товара нет>,
  "upsell_attempt": <true/false — пытался ли предложить дополнительный товар/услугу>,
  "answered_all_questions": <true/false — ответил ли на все вопросы клиента>,
  "outcome": <"продажа" | "отказ" | "без результата" | "консультация">,
  "overall_score": <число от 1 до 10>,
  "comment": "<короткий комментарий на русском, 1-2 предложения, что можно улучшить>"
}

Правила оценки:
- 9-10: идеальный диалог, всё отлично
- 7-8: хорошо, но есть мелкие замечания
- 5-6: средне, есть заметные проблемы
- 3-4: плохо, клиент скорее всего недоволен
- 1-2: очень плохо, грубость или полное игнорирование
"""


def calculate_response_time_minutes(messages):
    """Calculate time between first customer message and first manager reply."""
    first_customer_time = None
    first_manager_time = None
    for msg in messages:
        from_info = msg.get("from", {})
        msg_time = msg.get("created_at") or msg.get("time")
        if not msg_time:
            continue
        if from_info.get("type") == "customer" and first_customer_time is None:
            first_customer_time = msg_time
        elif from_info.get("type") in ("user", "manager", "operator") and first_manager_time is None:
            if first_customer_time:
                first_manager_time = msg_time
                break
    if first_customer_time and first_manager_time:
        try:
            t1 = datetime.fromisoformat(first_customer_time.replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(first_manager_time.replace("Z", "+00:00"))
            return max(0, round((t2 - t1).total_seconds() / 60, 1))
        except Exception:
            pass
    return None


def analyze_dialog_with_claude(dialog_text, api_key):
    """Send dialog to Claude for quality analysis. Returns dict."""
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=QUALITY_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Диалог менеджера с клиентом:\n\n{dialog_text}",
            }
        ],
    )
    raw = message.content[0].text.strip()
    # Extract JSON from response
    try:
        # Try direct parse
        return json.loads(raw)
    except json.JSONDecodeError:
        # Try to find JSON in the response
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start:end])
            except json.JSONDecodeError:
                pass
    logger.error("Failed to parse Claude response: %s", raw[:200])
    return None


async def fetch_closed_dialogs(mg_endpoint, mg_token, since_dt, until_dt):
    """Fetch dialogs closed between since_dt and until_dt."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Get closed dialogs
        resp = await client.get(
            f"{mg_endpoint}/api/bot/v1/dialogs",
            headers={"x-bot-token": mg_token},
            params={"is_active": "false", "limit": 100},
        )
        resp.raise_for_status()
        all_dialogs = resp.json()

    # Filter by close date
    result = []
    for d in all_dialogs:
        closed_at = d.get("closed_at")
        if not closed_at:
            continue
        try:
            closed_dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
            if since_dt <= closed_dt < until_dt:
                result.append(d)
        except Exception:
            continue
    return result


async def fetch_dialog_messages(mg_endpoint, mg_token, chat_id, limit=50):
    """Fetch messages for a chat."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{mg_endpoint}/api/bot/v1/messages",
            headers={"x-bot-token": mg_token},
            params={"chat_id": chat_id, "limit": limit},
        )
        resp.raise_for_status()
        return resp.json()


async def fetch_crm_users(retailcrm_url, api_key):
    """Fetch all CRM users (managers) and return id->name mapping."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{retailcrm_url}/api/v5/users",
            params={"apiKey": api_key, "limit": 100},
        )
        resp.raise_for_status()
        data = resp.json()
    users = {}
    for u in data.get("users", []):
        uid = u.get("id")
        name = f"{u.get('firstName', '')} {u.get('lastName', '')}".strip()
        if uid:
            users[uid] = name or f"User #{uid}"
    return users


def build_dialog_text(messages):
    """Build readable dialog text from messages."""
    lines = []
    for msg in messages:
        from_info = msg.get("from", {})
        from_type = from_info.get("type", "unknown")
        from_name = from_info.get("name", "")
        if from_type == "customer":
            role = f"Клиент ({from_name})" if from_name else "Клиент"
        elif from_type in ("user", "manager", "operator"):
            role = f"Менеджер ({from_name})" if from_name else "Менеджер"
        elif from_type == "bot":
            role = "Бот"
        else:
            role = from_type
        content = msg.get("content") or ""
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def run_quality_check(
    mg_endpoint, mg_token, retailcrm_url, retailcrm_api_key, anthropic_api_key,
    target_date=None,
):
    """
    Run quality check for dialogs closed on target_date (default: yesterday).
    Returns list of report rows (dicts).
    """
    if target_date is None:
        target_date = datetime.now(MSK).date() - timedelta(days=1)

    since_dt = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=MSK)
    until_dt = since_dt + timedelta(days=1)

    logger.info("Quality check for %s (from %s to %s)", target_date, since_dt, until_dt)

    # Fetch managers
    crm_users = await fetch_crm_users(retailcrm_url, retailcrm_api_key)
    # Also fetch MG Bot users mapping
    mg_user_map = {}

    # Fetch closed dialogs
    dialogs = await fetch_closed_dialogs(mg_endpoint, mg_token, since_dt, until_dt)
    logger.info("Found %d closed dialogs for %s", len(dialogs), target_date)

    rows = []
    for dialog in dialogs:
        chat_id = dialog.get("chat_id")
        dialog_id = dialog.get("id")
        responsible = dialog.get("responsible", {})
        manager_mg_id = responsible.get("id")
        manager_crm_ext_id = responsible.get("external_id")
        manager_name = crm_users.get(int(manager_crm_ext_id), f"ID:{manager_crm_ext_id}") if manager_crm_ext_id else "Не назначен"

        # Fetch messages
        try:
            messages = await fetch_dialog_messages(mg_endpoint, mg_token, chat_id)
        except Exception as exc:
            logger.error("Failed to fetch messages for chat #%d: %s", chat_id, exc)
            continue

        if not messages:
            continue

        # Get customer name
        customer_name = "Неизвестный"
        for msg in messages:
            from_info = msg.get("from", {})
            if from_info.get("type") == "customer":
                customer_name = from_info.get("name", "Неизвестный")
                break

        # Calculate response time
        response_time = calculate_response_time_minutes(messages)

        # Build dialog text and analyze
        dialog_text = build_dialog_text(messages)
        if len(dialog_text) < 10:
            continue

        analysis = analyze_dialog_with_claude(dialog_text, anthropic_api_key)
        if not analysis:
            continue

        row = {
            "date": str(target_date),
            "manager": manager_name,
            "customer": customer_name,
            "dialog_id": dialog_id,
            "response_time_min": response_time,
            "politeness": analysis.get("politeness"),
            "offered_alternative": "да" if analysis.get("offered_alternative") else "нет",
            "upsell_attempt": "да" if analysis.get("upsell_attempt") else "нет",
            "answered_all_questions": "да" if analysis.get("answered_all_questions") else "нет",
            "outcome": analysis.get("outcome", ""),
            "overall_score": analysis.get("overall_score"),
            "comment": analysis.get("comment", ""),
        }
        rows.append(row)
        logger.info("Dialog #%d: manager=%s score=%s", dialog_id, manager_name, row["overall_score"])

    return rows


def format_report_csv(rows):
    """Format rows as CSV string."""
    if not rows:
        return "Нет закрытых диалогов за этот период.\n"

    headers = ["Дата", "Менеджер", "Клиент", "ID диалога", "Скорость ответа (мин)",
               "Вежливость (1-10)", "Альтернатива", "Допродажа", "Все вопросы",
               "Итог", "Оценка (1-10)", "Комментарий"]

    lines = [",".join(headers)]
    for row in rows:
        values = [
            row["date"], row["manager"], row["customer"], str(row["dialog_id"]),
            str(row["response_time_min"] or "N/A"),
            str(row["politeness"] or ""), row["offered_alternative"],
            row["upsell_attempt"], row["answered_all_questions"],
            row["outcome"], str(row["overall_score"] or ""),
            f'"{row["comment"]}"',
        ]
        lines.append(",".join(values))

    # Summary by manager
    from collections import defaultdict
    manager_scores = defaultdict(list)
    for row in rows:
        if row["overall_score"]:
            manager_scores[row["manager"]].append(row["overall_score"])

    lines.append("")
    lines.append("СВОДКА ПО МЕНЕДЖЕРАМ")
    lines.append("Менеджер,Диалогов,Средний балл")
    for manager, scores in sorted(manager_scores.items()):
        avg = round(sum(scores) / len(scores), 1)
        lines.append(f"{manager},{len(scores)},{avg}")

    return "\n".join(lines)
