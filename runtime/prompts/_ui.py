"""Localized status lines every agent streams while it works ("Using X…").

Agent-neutral on purpose: this module imports nothing from a product agent, so
an agent that ships without the business prompts (the generic `custom` agent,
the demo agents) can still say what it is doing.
"""
from __future__ import annotations

# Localized UI messages
UI_MESSAGES = {
    "en": {
        "analyzing": "Analyzing your question...",
        "using_tool": "Using {tool_name}...",
        "retrieved_data": "Retrieved data from {tool_name}",
        "preparing_change": "Preparing: {tool_name}...",
        "prepared_change": "Ready to review: {tool_name}",
        "download_data": "Download complete data ({rows} rows):"
    },
    "ar": {
        "analyzing": "جاري تحليل سؤالك...",
        "using_tool": "جاري الاطلاع على {tool_name}...",
        "retrieved_data": "تم جلب {tool_name}",
        "preparing_change": "جاري تجهيز: {tool_name}...",
        "prepared_change": "جاهز للمراجعة: {tool_name}",
        "download_data": "تحميل البيانات الكاملة ({rows} صف):"
    },
    "tr": {
        "analyzing": "Sorunuz analiz ediliyor...",
        "using_tool": "{tool_name} kullanılıyor...",
        "retrieved_data": "{tool_name} verisi alındı",
        "download_data": "Tam veriyi indirin ({rows} satır):"
    },
    "ru": {
        "analyzing": "Анализ вашего вопроса...",
        "using_tool": "Использование {tool_name}...",
        "retrieved_data": "Данные получены из {tool_name}",
        "download_data": "Скачать полные данные ({rows} строк):"
    }
}


def get_ui_message(locale: str, key: str, **kwargs) -> str:
    """Get a localized UI message."""
    locale = locale if locale in UI_MESSAGES else "en"
    messages = UI_MESSAGES.get(locale, UI_MESSAGES["en"])
    message = messages.get(key, UI_MESSAGES["en"].get(key, ""))
    return message.format(**kwargs) if kwargs else message
