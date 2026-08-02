"""
Сервис генерации SEO-контента для карточки товара WB через Gemini API.
Использует Gemini 2.5 Flash (бесплатный tier).
"""

import logging
from typing import TYPE_CHECKING

import google.generativeai as genai

from config import settings
from services.wb_api import CompetitorProduct

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Инициализация клиента Gemini
genai.configure(api_key=settings.GEMINI_API_KEY)
_model = genai.GenerativeModel("gemini-2.5-flash")


SEO_SYSTEM_PROMPT = """Ты — эксперт по SEO-оптимизации карточек товаров на маркетплейсе Wildberries.
Твоя задача — создавать SEO-контент, который одновременно:
1. Соответствует поисковым запросам покупателей на WB
2. Удовлетворяет алгоритмам ранжирования Wildberries
3. Читается естественно и убедительно для покупателя

Алгоритм Wildberries учитывает:
- Точное вхождение ключевых слов в название (наибольший вес)
- Вхождение ключей в описание
- Релевантность категории
- Уникальность контента

Правила:
- Название: до 60 символов, главный ключ в начале
- Описание: 500-2000 символов, ключи вписаны органично
- Не используй символ | в названии
- Избегай переспама ключей (не более 3-4 вхождений одного слова)
- Не используй CAPS LOCK
"""


async def generate_seo_card(
    user_query: str,
    suggestions: list[str],
    competitors: list[CompetitorProduct],
) -> dict[str, str]:
    """
    Генерирует SEO-оптимизированную карточку товара.

    Args:
        user_query: исходный запрос пользователя (название/тип товара)
        suggestions: список ключевых слов из автоподсказок WB
        competitors: список товаров-конкурентов с их данными

    Returns:
        Словарь с полями: title, description, keywords
    """
    # Формируем контекст из данных конкурентов
    competitors_context = _build_competitors_context(competitors)
    keywords_context = _build_keywords_context(suggestions)

    prompt = f"""{SEO_SYSTEM_PROMPT}

---

## Задача

Создай SEO-карточку для товара по запросу: **"{user_query}"**

## Ключевые слова из поиска WB (автоподсказки покупателей)

{keywords_context}

## Анализ топ конкурентов в выдаче WB

{competitors_context}

---

## Что нужно создать

Верни ответ СТРОГО в следующем формате (без лишнего текста):

### НАЗВАНИЕ
[Название товара, до 60 символов, главный ключ в начале]

### ОПИСАНИЕ
[Продающее описание 500-2000 символов. Ключевые слова вписаны органично. Структурировано: УТП → характеристики → сценарии использования → призыв к действию]

### КЛЮЧЕВЫЕ СЛОВА
[Список из 15-25 ключевых слов через запятую, от высокочастотных к низкочастотным]

### АНАЛИЗ
[3-5 коротких инсайта о стратегии SEO конкурентов, что использовать и что избежать]
"""

    try:
        response = await _model.generate_content_async(
            prompt,
            generation_config=genai.GenerationConfig(
                temperature=0.7,
                max_output_tokens=2048,
            ),
        )
        raw_text = response.text
        return _parse_seo_response(raw_text)

    except Exception as e:
        logger.error("Ошибка генерации SEO через Gemini: %s", e)
        raise RuntimeError(f"Ошибка Gemini API: {e}") from e


def _build_competitors_context(competitors: list[CompetitorProduct]) -> str:
    """Форматирует данные конкурентов для промпта."""
    if not competitors:
        return "Конкуренты не найдены."

    lines: list[str] = []
    for i, c in enumerate(competitors[:8], start=1):
        desc_preview = c.description[:300] + "..." if len(c.description) > 300 else c.description
        lines.append(
            f"{i}. **{c.name}** (бренд: {c.brand}, категория: {c.subject_name})\n"
            f"   Описание: {desc_preview or 'нет описания'}"
        )
    return "\n\n".join(lines)


def _build_keywords_context(suggestions: list[str]) -> str:
    """Форматирует ключевые слова для промпта."""
    if not suggestions:
        return "Ключевые слова не найдены."
    return "\n".join(f"- {kw}" for kw in suggestions)


def _parse_seo_response(raw_text: str) -> dict[str, str]:
    """
    Парсит структурированный ответ Gemini в словарь.
    Если структура нарушена — возвращает весь текст в поле 'raw'.
    """
    sections = {
        "title": "",
        "description": "",
        "keywords": "",
        "analysis": "",
        "raw": "",
    }

    # Маркеры секций
    markers = {
        "### НАЗВАНИЕ": "title",
        "### ОПИСАНИЕ": "description",
        "### КЛЮЧЕВЫЕ СЛОВА": "keywords",
        "### АНАЛИЗ": "analysis",
    }

    current_section: str | None = None
    buffer: list[str] = []

    for line in raw_text.split("\n"):
        matched = False
        for marker, key in markers.items():
            if line.strip().startswith(marker):
                # Сохраняем предыдущую секцию
                if current_section:
                    sections[current_section] = "\n".join(buffer).strip()
                current_section = key
                buffer = []
                matched = True
                break
        if not matched and current_section:
            buffer.append(line)

    # Сохраняем последнюю секцию
    if current_section and buffer:
        sections[current_section] = "\n".join(buffer).strip()

    # Если не удалось распарсить — кладём весь текст в raw
    if not any([sections["title"], sections["description"], sections["keywords"]]):
        sections["raw"] = raw_text

    return sections
