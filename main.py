"""
main.py — Autotagger via MG Bot API
Tags dialogs with multiple tags. Checks customer history.
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
MG_BOT_TOKEN = os.environ["MG_BOT_TOKEN"]
MG_BOT_ENDPOINT = os.environ["MG_BOT_ENDPOINT"]

mg_client = MGBotClient(MG_BOT_ENDPOINT, MG_BOT_TOKEN)


async def process_chat(chat_id):
    logger.info("Processing chat #%d", chat_id)
    try:
        messages = await mg_client.get_chat_messages(chat_id, limit=30)
    except Exception as exc:
        logger.error("Failed to get messages for chat #%d: %s", chat_id, exc)
        return
    if not messages:
        return

    # Find active dialog
    dialog = await mg_client.get_active_dialog(chat_id)
    if not dialog:
        logger.warning("No dialog found for chat #%d", chat_id)
        return
    dialog_id = dialog.get("id")

    # Check if customer is new (first dialog ever)
    dialog_count = await mg_client.count_dialogs(chat_id)
    is_new_customer = dialog_count <= 1
    logger.info("Chat #%d: %d dialogs, is_new=%s", chat_id, dialog_count, is_new_customer)

    dialog_text = build_dialog_text(messages)
    try:
        tags = classify_dialog(dialog_text, ANTHROPIC_API_KEY, is_new_customer=is_new_customer)
        if not tags:
            logger.info("Chat #%d, dialog #%s -> skipped (not enough context)", chat_id, dialog_id)
            return
        logger.info("Chat #%d, dialog #%s -> tags: %s", chat_id, dialog_id, tags)
        await mg_client.add_dialog_tags(dialog_id, tags)
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
                        if from_info.get("type") == "customer" and chat_id:
                            asyncio.create_task(process_chat(int(chat_id)))
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

app = FastAPI(title="RetailCRM Autotagger", version="3.2.0", lifespan=lifespan)

@app.get("/")
async def health():
    return JSONResponse({"status": "ok", "version": "3.2.0", "mg_bot_endpoint": MG_BOT_ENDPOINT})
