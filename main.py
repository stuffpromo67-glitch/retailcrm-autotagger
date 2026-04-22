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
from sheets_writer import GoogleSheetsWriter

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
RETAILCRM_URL = os.getenv("RETAILCRM_URL", "")
RETAILCRM_API_KEY = os.getenv("RETAILCRM_API_KEY", "")
MG_BOT_TOKEN = os.environ["MG_BOT_TOKEN"]
MG_BOT_ENDPOINT = os.environ["MG_BOT_ENDPOINT"]
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")

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
            if GOOGLE_CREDS_JSON and GOOGLE_SHEET_ID:
                try:
                    sheets = GoogleSheetsWriter(GOOGLE_CREDS_JSON, GOOGLE_SHEET_ID)
                    await sheets.write_report(rows, str(datetime.now(MSK).date() - timedelta(days=1)))
                    logger.info("Report written to Google Sheet")
                except Exception as e:
                    logger.error("Failed to write to Google Sheet: %s", e)
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


app = FastAPI(title="RetailCRM Autotagger + Quality", version="5.1.0", lifespan=lifespan)


@app.get("/")
async def health():
    return JSONResponse({"status": "ok", "version": "5.0.0"})


_inflight_checks = {}


async def _run_and_persist(target_date):
    """Run the quality check for target_date and write to Google Sheet."""
    key = str(target_date)
    _inflight_checks[key] = {"status": "running", "started_at": datetime.now(MSK).isoformat()}
    try:
        logger.info("Manual quality check: running for %s", target_date)
        rows = await run_quality_check(
            MG_BOT_ENDPOINT, MG_BOT_TOKEN,
            RETAILCRM_URL, RETAILCRM_API_KEY,
            ANTHROPIC_API_KEY,
            target_date=target_date,
        )
        logger.info("Manual quality check: analyzed %d dialogs for %s", len(rows), target_date)
        wrote = False
        if GOOGLE_CREDS_JSON and GOOGLE_SHEET_ID:
            try:
                sheets = GoogleSheetsWriter(GOOGLE_CREDS_JSON, GOOGLE_SHEET_ID)
                await sheets.write_report(rows, str(target_date))
                wrote = True
                logger.info("Manual quality check: report written to Google Sheet")
            except Exception as e:
                logger.error("Failed to write to Google Sheet: %s", e)
        _inflight_checks[key] = {
            "status": "done",
            "rows": len(rows),
            "written_to_sheet": wrote,
            "finished_at": datetime.now(MSK).isoformat(),
        }
    except Exception as exc:
        logger.error("Manual quality check failed: %s", exc, exc_info=True)
        _inflight_checks[key] = {
            "status": "error",
            "error": str(exc),
            "finished_at": datetime.now(MSK).isoformat(),
        }


@app.post("/run-quality-check")
async def manual_quality_check(days_ago: int = 1):
    """Kick off quality check in background. Poll /quality-check-status?date=YYYY-MM-DD."""
    target_date = datetime.now(MSK).date() - timedelta(days=days_ago)
    asyncio.create_task(_run_and_persist(target_date))
    return JSONResponse({
        "status": "started",
        "target_date": str(target_date),
        "poll": f"/quality-check-status?date={target_date}",
    }, status_code=202)


@app.get("/quality-check-status")
async def quality_check_status(date: str = ""):
    """Return status of the most recent manual quality check for a given date."""
    if not date:
        return JSONResponse(_inflight_checks)
    return JSONResponse(_inflight_checks.get(date, {"status": "unknown"}))
