import asyncio
import html
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set

import aiohttp

logger = logging.getLogger("FamilyTasksBot.VoiceSummary")

ALLOWED_CATEGORIES: Set[str] = {
    "health",
    "household",
    "family",
    "shopping",
    "plans",
    "work",
    "other",
}

CATEGORY_NAMES_RU: Dict[str, str] = {
    "health": "Здоровье",
    "household": "Дом и быт",
    "family": "Семья",
    "shopping": "Покупки",
    "plans": "Планы",
    "work": "Работа",
    "other": "Разное",
}


def build_voice_summary_prompt(transcript: str) -> str:
    """
    Формирует системный промпт для резюмирования голосового сообщения жены с помощью Qwen.
    """
    return f"""Ты — чуткий и внимательный персональный секретарь семьи.
Твоя задача — составить краткое структурированное резюме входящего голосового сообщения от жены.

ТЕКСТ СООБЩЕНИЯ (ТРАНСКРИПЦИЯ):
\"\"\"{transcript}\"\"\"

ОБЩИЕ ПРАВИЛА:
1. Пойми смысл всего сообщения целиком и сохрани истинное намерение говорящей.
2. Извлеки главное, отсекая слова-паразиты, оговорки, повторы и эмоциональные междометия.
3. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО придумывать несуществующие факты или домысливать детали.
4. Сохраняй неопределенность ("где-то на неделе", "может быть", "точно не помню") и не делай приблизительные вещи точными.
5. Четко отличай фактические события от догадок, сомнений и предположений.
6. Обычные личные истории, эмоции или рассуждения НЕ являются задачами. Не формулируй их как задачи.
7. Ответ должен быть на грамотном русском языке.

СПЕЦИАЛЬНЫЕ ПРАВИЛА ДЛЯ СООБЩЕНИЙ О ЗДОРОВЬЕ И САМОЧУВСТВИИ (категория health):
Если жена говорит о здоровье, симптомах, самочувствии или врачах:
- Резюмируй ТОЛЬКО то, что она прямо озвучила.
- Зафиксируй конкретные симптомы, длительность, частоту и изменения состояния.
- Различай слова жены о симптомах и её собственные интерпретации.
- НЕ ставь медицинские диагнозы.
- НЕ предлагай и не рекомендуй лекарства, процедуры или лечение.
- НЕ придумывай медицинские термины и подробности.
- НЕ домысливай эмоциональные или психиатрические расстройства.
- Если формулировка расплывчата или неточна, явно сохрани эту неопределенность.

КАТЕГОРИИ (поле category):
- health: самочувствие, симптомы, здоровье, лекарства, визиты к врачу
- household: домашнее хозяйство, быт, уборка, ремонт, счета
- family: дети, школа, садик, родственники, семейные вопросы
- shopping: покупки, продукты, заказы, вещи
- plans: планы на вечер/выходные, встречи, досуг, поездки
- work: работа, учеба, деловые вопросы
- other: всё остальное, что не подошло ни под одну категорию

ФОРМАТ ОТВЕТА:
Верни СТРОГО валидный JSON-объект без markdown-разметки (без ```json), без тегов <think> и без лишних слов:
{{
  "summary": "Краткое естественное резюме сути сообщения (1-3 предложения)",
  "category": "health" | "household" | "family" | "shopping" | "plans" | "work" | "other",
  "key_points": [
    "Первый ключевой пункт или симптом",
    "Второй ключевой пункт или симптом"
  ]
}}
"""


def extract_and_parse_summary_json(raw_response: str) -> Dict[str, Any]:
    """
    Очищает ответ модели от тегов рассуждений (<thought>, <think>),
    markdown-тегов и безопасно извлекает JSON-объект.
    """
    cleaned = re.sub(
        r"<thought[^>]*>.*?</thought>",
        "",
        raw_response,
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = re.sub(
        r"<think[^>]*>.*?</think>",
        "",
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = re.sub(r"<thought[^>]*>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<think[^>]*>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()

    start_idx = cleaned.find("{")
    end_idx = cleaned.rfind("}")

    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        raise ValueError(f"В ответе Ollama не найден JSON-объект: {raw_response[:200]}")

    json_str = cleaned[start_idx : end_idx + 1].strip()
    data = json.loads(json_str)

    if not isinstance(data, dict):
        raise ValueError("Распарсенный JSON не является словарем.")

    # Нормализация полей
    summary = str(data.get("summary", "")).strip()
    category = str(data.get("category", "other")).strip().lower()
    if category not in ALLOWED_CATEGORIES:
        category = "other"

    raw_points = data.get("key_points", [])
    key_points: List[str] = []
    if isinstance(raw_points, list):
        for p in raw_points:
            p_str = str(p).strip()
            if p_str:
                key_points.append(p_str)

    return {
        "summary": summary,
        "category": category,
        "key_points": key_points,
    }


async def generate_voice_summary(
    transcript: str,
    ollama_url: Optional[str] = None,
    model: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Генерирует структурированное резюме голосового сообщения с помощью Ollama (Qwen 3.8).
    """
    if not transcript or not transcript.strip():
        logger.warning("Пустая транскрипция, генерация резюме пропущена.")
        return None

    # Чтение настроек по умолчанию из переменных окружения
    url = ollama_url or os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
    if not url.rstrip("/").endswith("/api/generate"):
        url = f"{url.rstrip('/')}/api/generate"

    model_name = model or os.getenv("OLLAMA_MODEL", "qwen3.8:27b-mlx")
    timeout_val = timeout_seconds or int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "120"))

    prompt = build_voice_summary_prompt(transcript)

    payload = {
        "model": model_name,
        "prompt": prompt,
        "stream": False,
    }

    logger.info("Генерация резюме голосового сообщения через Ollama (%s)...", model_name)

    client_timeout = aiohttp.ClientTimeout(total=timeout_val)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.post(url, json=payload) as response:
                if response.status != 200:
                    err_text = await response.text()
                    logger.error("Ollama вернула ошибку HTTP %d при резюмировании: %s", response.status, err_text)
                    return None

                response_data = await response.json()
                raw_response = response_data.get("response", "")

        parsed_summary = extract_and_parse_summary_json(raw_response)
        logger.info(
            "Резюме успешно сгенерировано. Категория: '%s', ключевых пунктов: %d.",
            parsed_summary["category"],
            len(parsed_summary["key_points"]),
        )
        return parsed_summary

    except asyncio.TimeoutError:
        logger.error("Таймаут обращения к Ollama при резюмировании (%d с).", timeout_val)
        return None
    except aiohttp.ClientError as e:
        logger.error("Сетевая ошибка при запросе резюме к Ollama: %s", type(e).__name__)
        return None
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("Ошибка при разборе JSON резюме от Ollama: %s", e)
        return None
    except Exception as e:
        logger.exception("Непредвиденная ошибка при генерации резюме: %s", type(e).__name__)
        return None


def format_summary_for_caption(summary_data: Dict[str, Any], max_len: int = 1024) -> str:
    """
    Форматирует структурированное резюме в красивый HTML-текст для Telegram caption.
    Гарантирует, что итоговый размер не превысит лимит Telegram в 1024 символа.
    """
    category = summary_data.get("category", "other")
    cat_title = CATEGORY_NAMES_RU.get(category, "Разное")
    summary_text = summary_data.get("summary", "").strip()
    key_points = summary_data.get("key_points", [])

    header = f"📝 <b>Резюме [{html.escape(cat_title)}]:</b>"
    blocks = [header]

    if summary_text:
        blocks.append(html.escape(summary_text))

    if key_points and isinstance(key_points, list):
        kp_lines = [f"• {html.escape(p)}" for p in key_points if str(p).strip()]
        if kp_lines:
            blocks.append("\n".join(kp_lines))

    result = "\n".join(blocks)
    if len(result) <= max_len:
        return result

    # Если превышает 1024 символа — сокращаем ключевые пункты или сам текст
    # Сначала пробуем оставить только заголовок и summary_text
    compact_blocks = [header]
    if summary_text:
        compact_blocks.append(html.escape(summary_text))
    result = "\n".join(compact_blocks)

    if len(result) <= max_len:
        return result

    # Если всё еще длинно — жестко обрезаем summary_text с многоточием
    available = max_len - len(header) - 5
    if available > 10:
        short_summary = html.escape(summary_text[:available]) + "..."
        return f"{header}\n{short_summary}"
    return header[:max_len]
