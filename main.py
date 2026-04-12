"""
main.py — Webhook-сервер для автотегирования диалогов RetailCRM
=========================================================

Запуск:
    uvicorn main:app --host 0.0.0.0 --port 8000

После запуска укажите URL вашего сервера в RetailCRM:
    Администрирование → Вебхуки → Добавить
    URL: https://ВАШ_ДОМЕН/webhook/conversation
    Событие: Новое сообщение / Создание диалога
"""

import hashlib
import hmac
import logging
import os
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from classifier import classify_dialog
from retailcrm_client import RetailCRMClient, build_dialog_text

# ---------------------------------------------------------------------------
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Конфигурация из переменных окружения
# ---------------------------------------------------------------------------
RETAILCRM_URL = os.environ["RETAILCRM_URL"]          # https://myshop.retailcrm.ru
RETAILCRM_API_KEY = os.environ["RETAILCRM_API_KEY"]  # API-ключ RetailCRM
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]  # Ключ Anthropic (Claude)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")     # Секрет для проверки подписи (опционально)

crm = RetailCRMClient(RETAILCRM_URL, RETAILCRM_API_KEY)

# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Сервер автотегирования запущен. Ожидаю вебхуки от RetailCRM…")
    yield
    logger.info("Сервер остановлен.")


app = FastAPI(
    title="RetailCRM Autotagger",
    description="Автоматическое тегирование диалогов с помощью ИИ",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def verify_signature(body: bytes, signature: str) -> bool:
    """
    Проверяет подпись вебхука RetailCRM (HMAC-SHA256).
    Если WEBHOOK_SECRET не задан — проверка пропускается.
    """
    if not WEBHOOK_SECRET:
        return True
    expected = hmac.new(
        WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def process_conversation(conversation_id: int) -> None:
    """
    Фоновая задача: получить сообщения диалога → классифицировать → поставить тег.
    Ошибки логируются и не роняют сервер.
    """
    logger.info("Обрабатываю диалог #%d…", conversation_id)

    try:
        # 1. Получаем сообщения диалога
        messages = crm.get_conversation_messages(conversation_id, limit=30)
    except Exception as exc:
        logger.error("Не удалось получить сообщения диалога #%d: %s", conversation_id, exc)
        return

    if not messages:
        logger.warning("Диалог #%d пустой — тег не проставляется.", conversation_id)
        return

    # 2. Формируем текст диалога
    dialog_text = build_dialog_text(messages)
    logger.info("Текст диалога #%d:\n%s", conversation_id, dialog_text[:300])

    try:
        # 3. Классифицируем через Claude
        tag = classify_dialog(dialog_text, ANTHROPIC_API_KEY)
        logger.info("Диалог #%d → тег «%s»", conversation_id, tag)

        # 4. Проставляем тег в RetailCRM
        crm.add_tag_to_conversation(conversation_id, tag)
    except Exception as exc:
        logger.error("Ошибка при классификации/тегировании диалога #%d: %s", conversation_id, exc)


# ---------------------------------------------------------------------------
# Эндпоинты
# ---------------------------------------------------------------------------

@app.post("/webhook/conversation")
async def webhook_conversation(request: Request, background_tasks: BackgroundTasks):
    """
    Принимает вебхук от RetailCRM о новом сообщении в диалоге.

    Как настроить вебхук в RetailCRM:
    1. Войдите в RetailCRM → Администрирование → Интеграции → Вебхуки
    2. Нажмите «Добавить вебхук»
    3. URL: https://ВАШ_ДОМЕН/webhook/conversation
    4. Выберите событие: «Новое входящее сообщение» (или «Изменение диалога»)
    5. Сохраните

    Сервер обрабатывает оба распространённых формата payload:
    - JSON с полем conversation.id
    - Form-encoded с полем data[conversation][id]
    """
    body = await request.body()

    # Проверка подписи (если настроена)
    signature = request.headers.get("X-RetailCRM-Signature", "")
    if WEBHOOK_SECRET and not verify_signature(body, signature):
        raise HTTPException(status_code=403, detail="Неверная подпись вебхука")

    # Разбираем payload
    content_type = request.headers.get("content-type", "")

    try:
        if "application/json" in content_type:
            payload = await request.json()
        else:
            # RetailCRM иногда шлёт form-urlencoded
            form = await request.form()
            payload = dict(form)
    except Exception as exc:
        logger.error("Не удалось разобрать payload: %s", exc)
        raise HTTPException(status_code=400, detail="Неверный формат данных")

    logger.info("Получен вебхук: %s", str(payload)[:500])

    # Извлекаем ID диалога из payload
    conversation_id = _extract_conversation_id(payload)

    if not conversation_id:
        logger.warning("conversation_id не найден в payload: %s", payload)
        # Возвращаем 200, чтобы RetailCRM не повторял запрос
        return JSONResponse({"ok": True, "message": "conversation_id не найден"})

    # Запускаем обработку в фоне, чтобы быстро ответить RetailCRM
    background_tasks.add_task(process_conversation, conversation_id)

    return JSONResponse({"ok": True, "conversation_id": conversation_id})


@app.post("/webhook/tag-now/{conversation_id}")
async def tag_now(conversation_id: int, background_tasks: BackgroundTasks):
    """
    Ручной запуск тегирования для конкретного диалога.
    Удобно для тестирования: POST /webhook/tag-now/123
    """
    background_tasks.add_task(process_conversation, conversation_id)
    return {"ok": True, "message": f"Тегирование диалога #{conversation_id} запущено"}


@app.get("/health")
async def health():
    """Проверка работоспособности сервера."""
    return {"status": "ok", "retailcrm_url": RETAILCRM_URL}


# ---------------------------------------------------------------------------
# Вспомогательный парсер payload
# ---------------------------------------------------------------------------

def _extract_conversation_id(payload: dict) -> int | None:
    """
    Ищет ID диалога в payload вебхука RetailCRM.
    RetailCRM использует несколько разных форматов в зависимости от версии.
    """

    # Вариант 1: JSON {"conversation": {"id": 123}}
    if "conversation" in payload and isinstance(payload["conversation"], dict):
        cid = payload["conversation"].get("id")
        if cid:
            return int(cid)

    # Вариант 2: JSON {"data": {"conversation": {"id": 123}}}
    data = payload.get("data", {})
    if isinstance(data, dict):
        conv = data.get("conversation", {})
        if isinstance(conv, dict) and conv.get("id"):
            return int(conv["id"])

    # Вариант 3: form-encoded "data[conversation][id]=123"
    if "data[conversation][id]" in payload:
        return int(payload["data[conversation][id]"])

    # Вариант 4: плоская структура {"conversationId": 123}
    for key in ("conversationId", "conversation_id", "id"):
        if key in payload:
            try:
                return int(payload[key])
            except (ValueError, TypeError):
                pass

    return None
