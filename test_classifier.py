"""
test_classifier.py — Тест классификатора без подключения к RetailCRM.

Запуск:
    python test_classifier.py

Требует: ANTHROPIC_API_KEY в .env
"""

import os
from dotenv import load_dotenv
from classifier import classify_dialog

load_dotenv()
api_key = os.environ["ANTHROPIC_API_KEY"]

# Тестовые диалоги → ожидаемые теги
TEST_CASES = [
    (
        "Клиент: Добрый день! Хочу вернуть куртку, она мне не подошла по размеру.",
        "возврат товара",
    ),
    (
        "Клиент: Здравствуйте, можно обменять синие кроссовки на такие же, но в красном цвете?",
        "обмен товара",
    ),
    (
        "Клиент: Добрый день! Мы представляем крупную торговую сеть и хотели бы обсудить оптовые поставки вашей продукции.",
        "запрос на сотрудничество",
    ),
    (
        "Клиент: Заработай 100000 рублей в день! Нажми на ссылку!!!",
        "спам",
    ),
    (
        "Клиент: Здравствуйте, сколько стоит доставка в Казань?",
        "новый клиент",
    ),
    (
        "Клиент: Когда будет ответ? Я уже час жду.",
        "ждет ответ",
    ),
]


def run_tests():
    print("=" * 60)
    print("  Тест классификатора диалогов RetailCRM")
    print("=" * 60)
    passed = 0
    for i, (dialog, expected_tag) in enumerate(TEST_CASES, 1):
        result = classify_dialog(dialog, api_key)
        status = "✅" if result == expected_tag else "❌"
        print(f"\nТест {i}: {status}")
        print(f"  Диалог:   {dialog[:80]}…")
        print(f"  Ожидалось: «{expected_tag}»")
        print(f"  Получено:  «{result}»")
        if result == expected_tag:
            passed += 1

    print("\n" + "=" * 60)
    print(f"  Результат: {passed}/{len(TEST_CASES)} тестов пройдено")
    print("=" * 60)


if __name__ == "__main__":
    run_tests()
