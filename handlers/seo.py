"""
Хэндлер основного сценария: получает текст от пользователя,
запускает сбор данных с WB и генерацию SEO через Gemini.
"""

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from config import settings
from services.gemini_seo import generate_seo_card
from services.wb_api import collect_competitors_data, get_suggestions

logger = logging.getLogger(__name__)
router = Router()


class SeoStates(StatesGroup):
    waiting_for_query = State()


# ──────────────────────────────────────────────
# Команда /seo  (альтернативный вход)
# ──────────────────────────────────────────────

@router.message(Command("seo"))
async def cmd_seo(message: Message, state: FSMContext) -> None:
    await state.set_state(SeoStates.waiting_for_query)
    await message.answer(
        "✏️ Введите название товара или поисковый запрос WB:",
        parse_mode="HTML",
    )


# ──────────────────────────────────────────────
# Обработка любого текстового сообщения
# ──────────────────────────────────────────────

@router.message(F.text)
async def handle_product_query(message: Message) -> None:
    """
    Основной хэндлер: принимает запрос, собирает данные с WB,
    генерирует SEO-карточку и отправляет результат пользователю.
    """
    query = message.text.strip()

    if len(query) < 3:
        await message.answer("⚠️ Запрос слишком короткий. Введите название товара.")
        return

    if len(query) > 200:
        await message.answer("⚠️ Запрос слишком длинный (максимум 200 символов).")
        return

    # Сообщаем о начале работы
    status_msg = await message.answer(
        f"🔍 Анализирую запрос: <b>{query}</b>\n\n"
        "⏳ Шаг 1/3: Получаю ключевые слова из WB...",
        parse_mode="HTML",
    )

    try:
        # ── Шаг 1: Ключевые слова из автоподсказок WB ──
        suggestions = await get_suggestions(query, count=settings.SUGGESTIONS_COUNT)

        await status_msg.edit_text(
            f"🔍 Анализирую запрос: <b>{query}</b>\n\n"
            f"✅ Шаг 1/3: Найдено {len(suggestions)} ключевых слов\n"
            "⏳ Шаг 2/3: Анализирую топ конкурентов...",
            parse_mode="HTML",
        )

        # ── Шаг 2: Данные конкурентов ──
        competitors = await collect_competitors_data(
            query, count=settings.COMPETITORS_COUNT
        )

        await status_msg.edit_text(
            f"🔍 Анализирую запрос: <b>{query}</b>\n\n"
            f"✅ Шаг 1/3: Найдено {len(suggestions)} ключевых слов\n"
            f"✅ Шаг 2/3: Проанализировано {len(competitors)} конкурентов\n"
            "⏳ Шаг 3/3: Генерирую SEO через Gemini AI...",
            parse_mode="HTML",
        )

        # ── Шаг 3: Генерация SEO через Gemini ──
        seo_data = await generate_seo_card(
            user_query=query,
            suggestions=suggestions,
            competitors=competitors,
        )

        # Удаляем статусное сообщение
        await status_msg.delete()

        # Отправляем результат
        await _send_seo_result(message, query, seo_data, len(suggestions), len(competitors))

    except Exception as e:
        logger.error("Ошибка при обработке запроса '%s': %s", query, e)
        await status_msg.edit_text(
            f"❌ <b>Произошла ошибка:</b> {e}\n\n"
            "Попробуйте ещё раз или измените запрос.",
            parse_mode="HTML",
        )


async def _send_seo_result(
    message: Message,
    query: str,
    seo_data: dict[str, str],
    keywords_count: int,
    competitors_count: int,
) -> None:
    """Форматирует и отправляет готовую SEO-карточку пользователю."""

    # Если Gemini вернул неструктурированный текст
    if seo_data.get("raw"):
        await message.answer(
            f"📋 <b>SEO-карточка для:</b> {query}\n\n{seo_data['raw']}",
            parse_mode="HTML",
        )
        return

    title = seo_data.get("title", "—")
    description = seo_data.get("description", "—")
    keywords = seo_data.get("keywords", "—")
    analysis = seo_data.get("analysis", "")

    # ── Сообщение 1: Название ──────────────────────
    title_len = len(title)
    title_indicator = "🟢" if title_len <= 60 else "🔴"
    await message.answer(
        f"✅ <b>SEO-карточка готова!</b>\n"
        f"<i>Запрос: {query} | Ключей: {keywords_count} | Конкурентов: {competitors_count}</i>\n"
        f"{'─' * 30}\n\n"
        f"📌 <b>НАЗВАНИЕ</b> {title_indicator} ({title_len}/60 символов)\n\n"
        f"<code>{title}</code>",
        parse_mode="HTML",
    )

    # ── Сообщение 2: Описание ──────────────────────
    desc_len = len(description)
    if desc_len < 500:
        desc_indicator = "🟡"
    elif desc_len <= 2000:
        desc_indicator = "🟢"
    else:
        desc_indicator = "🔴"

    # Разбиваем описание если оно длинное (лимит Telegram 4096 символов)
    desc_header = (
        f"📝 <b>ОПИСАНИЕ</b> {desc_indicator} ({desc_len} символов)\n\n"
    )
    if len(desc_header) + len(description) <= 4000:
        await message.answer(
            desc_header + description,
            parse_mode="HTML",
        )
    else:
        await message.answer(desc_header, parse_mode="HTML")
        # Разбиваем на чанки по 3800 символов
        for i in range(0, len(description), 3800):
            await message.answer(description[i:i + 3800])
            await asyncio.sleep(0.3)

    # ── Сообщение 3: Ключевые слова ───────────────
    kw_list = [kw.strip() for kw in keywords.split(",") if kw.strip()]
    formatted_keywords = "\n".join(f"• {kw}" for kw in kw_list)

    await message.answer(
        f"🔑 <b>КЛЮЧЕВЫЕ СЛОВА</b> ({len(kw_list)} шт.)\n\n"
        f"{formatted_keywords}",
        parse_mode="HTML",
    )

    # ── Сообщение 4: Аналитика конкурентов ────────
    if analysis:
        await message.answer(
            f"📊 <b>АНАЛИЗ КОНКУРЕНТОВ</b>\n\n{analysis}\n\n"
            f"{'─' * 30}\n"
            f"💡 Отправьте новый запрос для следующего товара",
            parse_mode="HTML",
        )
