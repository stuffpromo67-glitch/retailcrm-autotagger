# RetailCRM Autotagger — Инструкция по установке

## Что делает этот сервис

При каждом новом входящем сообщении в диалоге RetailCRM:
1. Сервер получает вебхук с ID диалога
2. Загружает историю сообщений через API
3. Отправляет текст в Claude (ИИ) для классификации
4. Автоматически проставляет один из 6 тегов:
   - **запрос на сотрудничество**
   - **возврат товара**
   - **обмен товара**
   - **спам**
   - **ждет ответ**
   - **новый клиент**

---

## Шаг 1. Установка зависимостей

```bash
pip install -r requirements.txt
```

## Шаг 2. Настройка переменных окружения

```bash
cp .env.example .env
```

Откройте `.env` и заполните:

| Переменная | Где взять |
|---|---|
| `RETAILCRM_URL` | URL вашего аккаунта, например `https://myshop.retailcrm.ru` |
| `RETAILCRM_API_KEY` | RetailCRM → Администрирование → Пользователи → API-ключи |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |

## Шаг 3. Тест классификатора

Перед запуском убедитесь, что ИИ правильно определяет теги:

```bash
python test_classifier.py
```

Должны пройти все 6 тестов.

## Шаг 4. Запуск сервера

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Для продакшна рекомендуется использовать systemd или Docker.

## Шаг 5. Настройка вебхука в RetailCRM

1. Войдите в RetailCRM
2. **Администрирование → Интеграции → Вебхуки**
3. Нажмите **«Добавить вебхук»**
4. Укажите:
   - **URL:** `https://ВАШ_ДОМЕН/webhook/conversation`
   - **Событие:** Новое входящее сообщение / Изменение диалога
5. Сохраните

> Если сервер запущен локально — используйте [ngrok](https://ngrok.com):
> ```bash
> ngrok http 8000
> # Скопируйте HTTPS URL из вывода ngrok
> ```

## Ручной запуск тегирования

Для тестирования конкретного диалога по его ID:

```bash
curl -X POST http://localhost:8000/webhook/tag-now/123
```

## Проверка работоспособности

```bash
curl http://localhost:8000/health
```

---

## Структура проекта

```
.
├── main.py              # FastAPI сервер (вебхуки)
├── classifier.py        # ИИ-классификатор (Claude API)
├── retailcrm_client.py  # Клиент RetailCRM API
├── test_classifier.py   # Тесты классификатора
├── requirements.txt     # Зависимости Python
└── .env.example         # Шаблон конфигурации
```

## Адаптация под вашу версию RetailCRM

Если эндпоинт `/api/v5/conversations/{id}/tag` недоступен в вашей версии,
сервер автоматически переключится на `/api/v5/conversations/{id}/edit`.

Если теги хранятся на заказе (а не на диалоге), используйте метод:
```python
crm.add_tag_to_order(order_id, tag)
```
в `main.py` вместо `crm.add_tag_to_conversation(...)`.
