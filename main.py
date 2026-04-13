"""
main.py — Autotagger + Quality Checker for RetailCRM
- WebSocket listener for auto-tagging dialogs
- Scheduled daily quality check at 9:00 MSK
- Manual endpoint POST /run-quality-check
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import websockets
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from classifier import classify_dialog
from mg_bot_client import MGBotClient, build_dialog_text
from quality_checker import run_quality_check, format_report_csv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
RETAILCRM_URL = os.getenv("RETAILCRM_URL", "")
RETAILCRM_API_KEY = os.getenv("RETAILCRM_API_KEY", "")
MG_BOT_TOKEN = os.environ["MG_BOT_TOKEN"]
MG_BOT_ENDPOINT = os.environ["MG_BOT_ENDPOINT"]

MSK = timezone(timedelta(hours=3))

mg_client = MGBotClient(MG_BOT_ENDPOINT, MG_BOT_TOKEN, retailcrm_url=RETAILCRM_URL, retailcrm_api_key=RETAILCRM_API_KEY)


# ---- Auto-tagging ----

async def process_chat(chat_id, mg_customer_id=None):
    logger.info("Processing chat #%d, mg_customer_id=%s", chat_id, mg_customer_id)
    try:
        messages = await mg_client.get_chat_messages(chat_id, limit=30)
    except Exception as exc:
        logger.error("Failed to get messages for chat #%d: %s", chat_id, exc)
        return
    if not messages:
        return
    if not mg_customer_id:
        for msg in messages:
            from_info = msg.get("from", {})
            if from_info.get("type") == "customer":
                mg_customer_id = from_info.get("id")
                break
    if not mg_customer_id:
        return
    crm_customer_id = await mg_client.find_crm_customer_by_mg_id(mg_customer_id)
    if not crm_customer_id:
        return
    dialog_count = await mg_client.count_dialogs(chat_id)
    is_new_customer = dialog_count <= 1
    dialog_text = build_dialog_text(messages)
    try:
        tags = classify_dialog(dialog_text, ANTHROPIC_API_KEY, is_new_customer=is_new_customer)
        if not tags:
            logger.info("Chat #%d -> skipped (not enough context)", chat_id)
            return
        logger.info("Chat #%d, CRM customer %s -> tags: %s", chat_id, crm_customer_id, tags)
        await mg_client.set_customer_tags_attached(crm_customer_id, tags)
    except Exception as exc:
        logger.error("Error classifying chat #%d: %s", chat_id, exc)


async def ws_listener():
    ws_url = mg_client.ws_url
    logger.info("Connecting to MG Bot WebSocket: %s", ws_url)
    while True:
        try:
            async with websockets.connect(
                ws_url,
                additional_headers={"x-bot-token": MG_BOT_TOKEN},
                ping_interval=30, ping_timeout=10,
            ) as ws:
                logger.info("Connected to MG Bot WebSocket")
                async for raw in ws:
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "message_new":
                        data = event.get("data", {})
                        msg = data.get("message", {})
                        chat_id = msg.get("chat_id")
                        from_info = msg.get("from", {})
                        mg_customer_id = None
                        if from_info.get("type") == "customer":
                            mg_customer_id = from_info.get("id")
                        if from_info.get("type") == "customer" and chat_id:
                            asyncio.create_task(process_chat(int(chat_id), mg_customer_id))
        except (websockets.ConnectionClosed, OSError) as exc:
            logger.warning("WebSocket disconnected: %s, reconnecting in 5s", exc)
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("WebSocket error: %s, reconnecting in 10s", exc)
            await asyncio.sleep(10)


# ---- Daily quality check scheduler ----

async def daily_quality_scheduler():
    """Run quality check every day at 9:00 MSK."""
    while True:
        now = datetime.now(MSK)
        target = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        logger.info("Next quality check at %s MSK (in %.0f min)", target.strftime("%Y-%m-%d %H:%M"), wait_seconds / 60)
        await asyncio.sleep(wait_seconds)

        try:
            logger.info("Starting scheduled quality check...")
            rows = await run_quality_check(
                MG_BOT_ENDPOINT, MG_BOT_TOKEN,
                RETAILCRM_URL, RETAILCRM_API_KEY,
                ANTHROPIC_API_KEY,
            )
            report = format_report_csv(rows)
            logger.info("Quality report:\n%s", report)
        except Exception as exc:
            logger.error("Quality check failed: %s", exc)


# ---- Lifespan ----

@asynccontextmanager
async def lifespan(app):
    ws_task = asyncio.create_task(ws_listener())
    quality_task = asyncio.create_task(daily_quality_scheduler())
    yield
    ws_task.cancel()
    quality_task.cancel()
    try:
        await ws_task
    except asyncio.CancelledError:
        pass
    try:
        await quality_task
    except asyncio.CancelledError:
        pass
    await mg_client.close()


app = FastAPI(title="RetailCRM Autotagger + Quality", version="5.0.0", lifespan=lifespan)


@app.get("/")
async def health():
    return JSONResponse({"status": "ok", "version": "5.0.0"})


@app.post("/run-quality-check")
async def manual_quality_check(days_ago: int = 1):
    """Manually trigger quality check. ?days_ago=1 for yesterday (default)."""
    target_date = datetime.now(MSK).date() - timedelta(days=days_ago)
    try:
        rows = await run_quality_check(
            MG_BOT_ENDPOINT, MG_BOT_TOKEN,
            RETAILCRM_URL, RETAILCRM_API_KEY,
            ANTHROPIC_API_KEY,
            target_date=target_date,
        )
        report = format_report_csv(rows)
        return PlainTextResponse(report, media_type="text/csv; charset=utf-8")
    except Exception as exc:
        logger.error("Manual quality check failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)
