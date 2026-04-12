"""
main.py — Autotagger via MG Bot API + RetailCRM API v5
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

import websockets
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from classifier import classify_dialog
from mg_bot_client import MGBotClient, build_dialog_text

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
RETAILCRM_URL = os.getenv("RETAILCRM_URL", "")
RETAILCRM_API_KEY = os.getenv("RETAILCRM_API_KEY", "")
MG_BOT_TOKEN = os.environ["MG_BOT_TOKEN"]
MG_BOT_ENDPOINT = os.environ["MG_BOT_ENDPOINT"]

mg_client = MGBotClient(MG_BOT_ENDPOINT, MG_BOT_TOKEN, retailcrm_url=RETAILCRM_URL, retailcrm_api_key=RETAILCRM_API_KEY)


async def process_chat(chat_id, mg_customer_id=None):
    logger.info("Processing chat #%d, mg_customer_id=%s", chat_id, mg_customer_id)
    try:
        messages = await mg_client.get_chat_messages(chat_id, limit=30)
    except Exception as exc:
        logger.error("Failed to get messages for chat #%d: %s", chat_id, exc)
        return
    if not messages:
        return

    # Extract mg_customer_id from first customer message if not provided
    if not mg_customer_id:
        for msg in messages:
            from_info = msg.get("from", {})
            if from_info.get("type") == "customer":
                mg_customer_id = from_info.get("id")
                break

    if not mg_customer_id:
        logger.warning("No customer found in chat #%d", chat_id)
        return

    # Find real RetailCRM customer ID
    crm_customer_id = await mg_client.find_crm_customer_by_mg_id(mg_customer_id)
    if not crm_customer_id:
        logger.error("CRM customer not found for mg_customer_id=%s", mg_customer_id)
        return

    dialog_text = build_dialog_text(messages)
    try:
        tag = classify_dialog(dialog_text, ANTHROPIC_API_KEY)
        logger.info("Chat #%d -> tag: %s (CRM customer %s)", chat_id, tag, crm_customer_id)
        await mg_client.set_customer_tag(crm_customer_id, tag)
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
                        # Get MG customer ID from message sender
                        from_info = msg.get("from", {})
                        mg_customer_id = None
                        if from_info.get("type") == "customer":
                            mg_customer_id = from_info.get("id")
                        if chat_id:
                            asyncio.create_task(process_chat(int(chat_id), mg_customer_id))
        except (websockets.ConnectionClosed, OSError) as exc:
            logger.warning("WebSocket disconnected: %s, reconnecting in 5s", exc)
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("WebSocket error: %s, reconnecting in 10s", exc)
            await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(ws_listener())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await mg_client.close()

app = FastAPI(title="RetailCRM Autotagger", version="2.2.0", lifespan=lifespan)

@app.get("/")
async def health():
    return JSONResponse({"status": "ok", "version": "2.2.0", "mg_bot_endpoint": MG_BOT_ENDPOINT})
