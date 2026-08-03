"""
Хэндлер основного сценария: получает текст от пользователя,
запускает сбор данных с WB и генерацию SEO через Gemini.

Поддерживает WB Partner API (если WB_API_TOKEN задан в .env):
- Частотность ключевых слов
- Загрузка готовой карточки на WB (/upload)
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
from services.wb_partner_api import (
    get_search_query_stats,
    is_wb_api_available,
    upload_card_to_wb,
)

logger = logging.getLogger(__name__)
router = Router()


class SeoStates(StatesGroup):
    waiting_for_query = State()
    waiting_for_upload_nm = State()
    waiting_for_upload_confirm = State()


# Временное хранилище последней сгенерированной карточки (per-user, in-memory)
# Формат: {user_id: {"title": ..., "description": ...}}
_last_card: dict[int, dict[str, str]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# /start и /help
# ─────────────────────────────────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    wb_api_status = "✅ подключён" if is_wb_api_available() else "❌ не настроен"
    await message.answer(
        "<b>🤖 WB SEO Bot — справка</b>\n\n"
        "<b>Команды:</b>\n"
        "• Просто напишите название товара — запустит генерацию SEO\n"
        "• /upload — загрузить последнюю карточку на WB\n"
        "• /help — эта справка\n\n"
        "<b>Как работает:</b>\n"
        "1️⃣ Ключевые слова из автоподсказок WB\n"
        "2️⃣ Анализ описаний топ-конкурентов\n"
        "3️⃣ Генерация продающего SEO через Gemini AI\n\n"
        f"<b>WB Partner API:</b> {wb_api_status}\n"
        + (
            "   (частотность ключей + загрузка карточки)\n"
            if is_wb_api_available()
            else "   Добавь WB_API_TOKEN в .env для расширенных функций\n"
        ),
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /upload — загрузка карточки на WB
# ─────────────────────────────────────────────────────────────────────────────

@router.message(Command("upload"))
async def cmd_upload(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]

    if not is_wb_api_available():
        await message.answer(
            "❌ <b>WB Partner API не настроен.</b>\n\n"
            "Чтобы загружать карточки напрямую на WB:\n"
            "1. Получи токен: <a href='https://seller.wildberries.ru/supplier-settings/access-to-api'>WB Кабинет → Настройки → Доступ к API</a>\n"
            "2. Добавь в .env: <code>WB_API_TOKEN=твой_токен</code>\n"
            "3. Перезапусти бота",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    card = _last_card.get(user_id)
    if not card:
        await message.answer(
            "⚠️ Нет сохранённой карточки.\n"
            "Сначала сгенерируй SEO — просто напиши название товара.",
        )
        return

    await state.set_state(SeoStates.waiting_for_upload_nm)
    await state.update_data(card=card)
    await message.answer(
        "📦 <b>Загрузка карточки на WB</b>\n\n"
        "Введи <b>артикул WB (nm_id)</b> — числовой идентификатор товара в твоём кабинете.\n\n"
        "<i>Найти: WB кабинет → Товары → Карточки товаров → столбец «Артикул WB»</i>",
        parse_mode="HTML",
    )


@router.message(SeoStates.waiting_for_upload_nm)
async def handle_upload_nm(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("⚠️ Введи числовой артикул WB (например: 123456789)")
        return

    nm_id = int(text)
    data = await state.get_data()
    card: dict[str, str] = data["card"]

    await state.update_data(nm_id=nm_id)
    await state.set_state(SeoStates.waiting_for_upload_confirm)

    title = card.get("title", "")
    await message.answer(
        f"✅ <b>Подтверди загрузку</b>\n\n"
        f"Артикул: <code>{nm_id}</code>\n"
        f"Название: <code>{title}</code>\n\n"
        "Напиши <b>да</b> для загрузки или <b>нет</b> для отмены.",
        parse_mode="HTML",
    )


@router.message(SeoStates.waiting_for_upload_confirm)
async def handle_upload_confirm(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip().lower()

    if text not in ("да", "yes", "y", "д"):
        await state.clear()
        await message.answer("❌ Загрузка отменена.")
        return

    data = await state.get_data()
    nm_id: int = data["nm_id"]
    card: dict[str, str] = data["card"]

    wait_msg = await message.answer("⏳ Загружаю карточку на WB...")

    success, result_msg = await upload_card_to_wb(
        nm_id=nm_id,
        title=card.get("title", ""),
        description=card.get("description", ""),
    )

    await wait_msg.delete()
    await message.answer(result_msg, parse_mode="HTML")
    await state.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Основной хэндлер: генерация SEO
# ─────────────────────────────────────────────────────────────────────────────

@router.message(F.text)
async def handle_product_query(message: Message, state: FSMContext) -> None:
    """
    Принимает запрос, собирает данные с WB, генерирует SEO-карточку.

    Шаги:
    1. WB автоподсказки → ключевые слова
    2. (опц.) WB Partner API → частотность ключей
    3. WB Search + card.wb.ru → данные конкурентов
    4. Gemini → SEO карточка
    """
    # Если пользователь в каком-то состоянии FSM — не перехватываем
    current_state = await state.get_state()
    if current_state is not None:
        return

    query = (message.text or "").strip()

    if len(query) < 3:
        await message.answer("⚠️ Запрос слишком короткий. Введите название товара.")
        return

    if len(query) > 200:
        await message.answer("⚠️ Запрос слишком длинный (максимум 200 символов).")
        return

    use_partner_api = is_wb_api_available()
    steps_total = 4 if use_partner_api else 3

    status_msg = await message.answer(
        f"🔍 Анализирую: <b>{query}</b>\n\n"
        f"⏳ Шаг 1/{steps_total}: Получаю ключевые слова из WB...",
        parse_mode="HTML",
    )

    try:
        # ── Шаг 1: Ключевые слова ────────────────────────────────────────────
        suggestions = await get_suggestions(query, count=settings.SUGGESTIONS_COUNT)

        step = 2
        await status_msg.edit_text(
            f"🔍 Анализирую: <b>{query}</b>\n\n"
            f"✅ Шаг 1/{steps_total}: Найдено {len(suggestions)} ключевых слов\n"
            + (
                f"⏳ Шаг {step}/{steps_total}: Получаю частотность из WB API...\n"
                if use_partner_api
                else f"⏳ Шаг {step}/{steps_total}: Анализирую топ конкурентов...\n"
            ),
            parse_mode="HTML",
        )

        # ── Шаг 2 (опц.): Частотность из WB Partner API ──────────────────────
        keyword_stats: dict[str, int] = {}
        if use_partner_api:
            keyword_stats = await get_search_query_stats(suggestions)
            step = 3
            await status_msg.edit_text(
                f"🔍 Анализирую: <b>{query}</b>\n\n"
                f"✅ Шаг 1/{steps_total}: Найдено {len(suggestions)} ключевых слов\n"
                f"✅ Шаг 2/{steps_total}: Частотность для {len(keyword_stats)} ключей\n"
                f"⏳ Шаг {step}/{steps_total}: Анализирую топ конкурентов...\n",
                parse_mode="HTML",
            )

        # ── Шаг 3: Анализ конкурентов ─────────────────────────────────────────
        competitors = await collect_competitors_data(
            query, count=settings.COMPETITORS_COUNT
        )

        step += 1
        await status_msg.edit_text(
            f"🔍 Анализирую: <b>{query}</b>\n\n"
            f"✅ Шаг 1/{steps_total}: Найдено {len(suggestions)} ключевых слов\n"
            + (f"✅ Шаг 2/{steps_total}: Частотность для {len(keyword_stats)} ключей\n" if use_partner_api else "")
            + f"✅ Шаг {step - 1}/{steps_total}: Проанализировано {len(competitors)} конкурентов\n"
            f"⏳ Шаг {step}/{steps_total}: Генерирую SEO через Gemini AI...",
            parse_mode="HTML",
        )

        # ── Шаг 4: Генерация через Gemini ─────────────────────────────────────
        seo_data = await generate_seo_card(
            user_query=query,
            suggestions=suggestions,
            competitors=competitors,
            keyword_stats=keyword_stats if keyword_stats else None,
        )

        await status_msg.delete()

        # Сохраняем карточку для возможной загрузки на WB
        user_id = message.from_user.id  # type: ignore[union-attr]
        _last_card[user_id] = {
            "title": seo_data.get("title", ""),
            "description": seo_data.get("description", ""),
        }

        await _send_seo_result(
            message=message,
            query=query,
            seo_data=seo_data,
            keywords_count=len(suggestions),
            competitors_count=len(competitors),
            has_freq_data=bool(keyword_stats),
        )

    except Exception as e:
        logger.error("Ошибка при обработке запроса '%s': %s", query, e, exc_info=True)
        try:
            await status_msg.edit_text(
                f"❌ <b>Произошла ошибка:</b>\n<code>{e}</code>\n\n"
                "Попробуйте ещё раз или измените запрос.",
                parse_mode="HTML",
            )
        except Exception:
            await message.answer(f"❌ Ошибка: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Форматирование и отправка результата
# ─────────────────────────────────────────────────────────────────────────────

async def _send_seo_result(
    message: Message,
    query: str,
    seo_data: dict[str, str],
    keywords_count: int,
    competitors_count: int,
    has_freq_data: bool = False,
) -> None:
    """Форматирует и отправляет готовую SEO-карточку пользователю (4 сообщения)."""

    # Если Gemini вернул нераспарсенный текст
    if seo_data.get("raw"):
        await message.answer(
            f"📋 <b>SEO-карточка:</b> {query}\n\n{seo_data['raw']}",
            parse_mode="HTML",
        )
        return

    title = seo_data.get("title", "—")
    description = seo_data.get("description", "—")
    keywords = seo_data.get("keywords", "—")
    analysis = seo_data.get("analysis", "")

    freq_badge = " | 📊 частотность WB" if has_freq_data else ""

    # ── 1. Шапка + Название ───────────────────────────────────────────────────
    title_len = len(title)
    title_indicator = "🟢" if title_len <= 60 else "🔴"
    await message.answer(
        f"✅ <b>SEO-карточка готова!</b>\n"
        f"<i>{query} · {keywords_count} ключей · {competitors_count} конкурентов{freq_badge}</i>\n"
        f"{'─' * 32}\n\n"
        f"📌 <b>НАЗВАНИЕ</b> {title_indicator} ({title_len}/60 симв.)\n\n"
        f"<code>{title}</code>",
        parse_mode="HTML",
    )

    # ── 2. Описание ───────────────────────────────────────────────────────────
    desc_len = len(description)
    desc_indicator = "🟡" if desc_len < 800 else ("🟢" if desc_len <= 2000 else "🔴")
    desc_header = f"📝 <b>ОПИСАНИЕ</b> {desc_indicator} ({desc_len} симв.)\n\n"

    if len(desc_header) + desc_len <= 4000:
        await message.answer(desc_header + description, parse_mode="HTML")
    else:
        await message.answer(desc_header, parse_mode="HTML")
        for i in range(0, desc_len, 3800):
            await message.answer(description[i : i + 3800])
            await asyncio.sleep(0.3)

    # ── 3. Ключевые слова ─────────────────────────────────────────────────────
    kw_list = [kw.strip() for kw in keywords.split(",") if kw.strip()]
    formatted_keywords = "\n".join(f"• {kw}" for kw in kw_list)
    await message.answer(
        f"🔑 <b>КЛЮЧЕВЫЕ СЛОВА</b> ({len(kw_list)} шт.)\n\n{formatted_keywords}",
        parse_mode="HTML",
    )

    # ── 4. Анализ конкурентов + подсказка ─────────────────────────────────────
    upload_hint = "\n\n💾 /upload — загрузить карточку на WB" if is_wb_api_available() else ""
    if analysis:
        await message.answer(
            f"📊 <b>АНАЛИЗ КОНКУРЕНТОВ</b>\n\n{analysis}"
            f"\n{'─' * 32}\n"
            f"💡 Напишите новый запрос для следующего товара{upload_hint}",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"{'─' * 32}\n"
            f"💡 Напишите новый запрос для следующего товара{upload_hint}",
            parse_mode="HTML",
        )
