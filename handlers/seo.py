"""
Хэндлер SEO-генерации с inline-кнопками.

Флоу:
1. Пользователь → текст запроса
2. Бот собирает данные WB (catalog, suggests) — показывает прогресс
3. Бот показывает сводку + кнопки выбора: Описание / Ключи / Анализ / Всё
4. Пользователь нажимает кнопку
5. Бот загружает описания конкурентов (если нужно) + генерирует через Gemini
"""

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import settings
from services.gemini_seo import GenerateMode, generate_seo_card
from services.wb_api import (
    CompetitorBasic,
    CompetitorProduct,
    build_suggestions,
    load_descriptions,
    search_competitors_basic,
)
from services.wb_partner_api import (
    get_search_query_stats,
    is_wb_api_available,
    upload_card_to_wb,
)

logger = logging.getLogger(__name__)
router = Router()

# In-memory хранилище данных сессии (user_id → данные)
_sessions: dict[int, dict] = {}


# ─────────────────────────────────────────────────────────────────────────────
# FSM
# ─────────────────────────────────────────────────────────────────────────────

class SeoStates(StatesGroup):
    waiting_for_upload_nm = State()
    waiting_for_upload_confirm = State()


# ─────────────────────────────────────────────────────────────────────────────
# Кнопки
# ─────────────────────────────────────────────────────────────────────────────

def _action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📝 Описание", callback_data="gen:description"),
            InlineKeyboardButton(text="🔑 Ключевые слова", callback_data="gen:keywords"),
        ],
        [
            InlineKeyboardButton(text="📊 Анализ конкурентов", callback_data="gen:analysis"),
            InlineKeyboardButton(text="✅ Полная карточка", callback_data="gen:all"),
        ],
    ])


def _regenerate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📝 Описание", callback_data="gen:description"),
            InlineKeyboardButton(text="🔑 Ключевые слова", callback_data="gen:keywords"),
        ],
        [
            InlineKeyboardButton(text="📊 Анализ", callback_data="gen:analysis"),
            InlineKeyboardButton(text="🔄 Перегенерировать", callback_data="gen:all"),
        ],
    ])


# ─────────────────────────────────────────────────────────────────────────────
# /help
# ─────────────────────────────────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    wb_badge = "✦ WB Partner API подключён" if is_wb_api_available() else "◦ WB Partner API не настроен"
    await message.answer(
        "<b>WB SEO Bot</b>\n\n"
        "Напишите название товара — получите SEO-карточку.\n\n"
        "<b>Команды</b>\n"
        "/upload — загрузить карточку напрямую на WB\n"
        "/help — справка\n\n"
        f"<i>{wb_badge}</i>",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /upload
# ─────────────────────────────────────────────────────────────────────────────

@router.message(Command("upload"))
async def cmd_upload(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]

    if not is_wb_api_available():
        await message.answer(
            "<b>WB Partner API не настроен</b>\n\n"
            "Для загрузки карточек добавь токен:\n"
            "1. <a href='https://seller.wildberries.ru/supplier-settings/access-to-api'>"
            "WB Кабинет → Настройки → Доступ к API</a>\n"
            "2. <code>WB_API_TOKEN=...</code> в файл .env\n"
            "3. Перезапусти бота",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    session = _sessions.get(user_id, {})
    card = session.get("last_card")
    if not card:
        await message.answer("Нет сохранённой карточки. Сначала сгенерируй SEO.")
        return

    await state.set_state(SeoStates.waiting_for_upload_nm)
    await state.update_data(card=card)
    await message.answer(
        "<b>Загрузка на WB</b>\n\n"
        "Введи артикул WB (nm_id) из своего кабинета:\n"
        "<i>WB кабинет → Товары → Карточки товаров → «Артикул WB»</i>",
        parse_mode="HTML",
    )


@router.message(SeoStates.waiting_for_upload_nm)
async def handle_upload_nm(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer(
            "Введи числовой артикул, например: <code>123456789</code>",
            parse_mode="HTML",
        )
        return
    nm_id = int(text)
    data = await state.get_data()
    await state.update_data(nm_id=nm_id)
    await state.set_state(SeoStates.waiting_for_upload_confirm)
    title = data["card"].get("title", "—")
    await message.answer(
        f"Артикул: <code>{nm_id}</code>\n"
        f"Название: <code>{title}</code>\n\n"
        "Загрузить? <b>да</b> / <b>нет</b>",
        parse_mode="HTML",
    )


@router.message(SeoStates.waiting_for_upload_confirm)
async def handle_upload_confirm(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip().lower() not in ("да", "yes", "д", "y"):
        await state.clear()
        await message.answer("Отменено.")
        return
    data = await state.get_data()
    wait = await message.answer("Загружаю на WB…")
    success, msg = await upload_card_to_wb(
        nm_id=data["nm_id"],
        title=data["card"].get("title", ""),
        description=data["card"].get("description", ""),
    )
    await wait.delete()
    await message.answer(msg, parse_mode="HTML")
    await state.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Основной хэндлер: сбор данных WB
# ─────────────────────────────────────────────────────────────────────────────

@router.message(F.text)
async def handle_product_query(message: Message, state: FSMContext) -> None:
    if await state.get_state() is not None:
        return

    query = (message.text or "").strip()
    if len(query) < 3:
        await message.answer("Запрос слишком короткий.")
        return
    if len(query) > 200:
        await message.answer("Запрос слишком длинный (максимум 200 символов).")
        return

    user_id = message.from_user.id  # type: ignore[union-attr]
    status = await message.answer(_progress(1, query))

    try:
        # ── Сбор данных WB ───────────────────────────────────────────────────
        competitors_basic, preset, norm_query = await search_competitors_basic(
            query, limit=settings.COMPETITORS_COUNT
        )
        await status.edit_text(_progress(2, query, comp_count=len(competitors_basic)))

        suggestions = build_suggestions(
            query=query,
            norm_query=norm_query,
            competitors=competitors_basic,
            count=settings.SUGGESTIONS_COUNT,
        )

        # WB Partner API: частотность (опц.)
        keyword_stats: dict[str, int] = {}
        if is_wb_api_available():
            await status.edit_text(_progress(3, query, comp_count=len(competitors_basic), kw_count=len(suggestions)))
            keyword_stats = await get_search_query_stats(suggestions)

        # ── Сохраняем сессию ─────────────────────────────────────────────────
        _sessions[user_id] = {
            "query": query,
            "suggestions": suggestions,
            "keyword_stats": keyword_stats,
            "competitors_basic": [c.to_dict() for c in competitors_basic],
            "competitors_full": None,  # загружается по запросу
            "last_card": None,
        }

        await status.delete()

        # ── Сводка + кнопки выбора ───────────────────────────────────────────
        wb_info = _wb_info_block(
            query=query,
            preset=preset,
            norm_query=norm_query,
            comp_count=len(competitors_basic),
            kw_count=len(suggestions),
            has_freq=bool(keyword_stats),
        )
        await message.answer(
            wb_info,
            parse_mode="HTML",
            reply_markup=_action_keyboard(),
        )

    except Exception as e:
        logger.error("Ошибка WB для '%s': %s", query, e, exc_info=True)
        try:
            await status.edit_text(
                f"<b>Ошибка при сборе данных</b>\n\n<code>{e}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Callback: кнопки выбора генерации
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("gen:"))
async def handle_generate(callback: CallbackQuery) -> None:
    await callback.answer()

    user_id = callback.from_user.id  # type: ignore[union-attr]
    session = _sessions.get(user_id)
    if not session:
        await callback.message.answer("Сессия истекла. Отправь запрос снова.")  # type: ignore[union-attr]
        return

    mode: GenerateMode = callback.data.split(":")[1]  # type: ignore[union-attr]
    query = session["query"]
    suggestions = session["suggestions"]
    keyword_stats = session["keyword_stats"]

    needs_descriptions = mode in ("description", "analysis", "all")

    gen_msg = await callback.message.answer(  # type: ignore[union-attr]
        _generating_msg(mode, needs_descriptions),
        parse_mode="HTML",
    )

    try:
        # ── Загружаем описания если нужны ────────────────────────────────────
        competitors: list[CompetitorProduct]
        if needs_descriptions:
            if session.get("competitors_full") is None:
                basics = [CompetitorBasic.from_dict(d) for d in session["competitors_basic"]]
                if basics:
                    full = await load_descriptions(basics)
                    session["competitors_full"] = [
                        {
                            "nm_id": c.nm_id, "name": c.name, "brand": c.brand,
                            "subject_name": c.subject_name,
                            "keywords_from_name": c.keywords_from_name,
                            "description": c.description,
                        }
                        for c in full
                    ]
                else:
                    session["competitors_full"] = []

            competitors = [
                CompetitorProduct(**d)
                for d in (session["competitors_full"] or [])
            ]
        else:
            # Для ключей описания не нужны — используем базовые данные
            competitors = [
                CompetitorProduct(description="", **d)
                for d in session["competitors_basic"]
            ]

        # ── Генерация ─────────────────────────────────────────────────────────
        seo_data = await generate_seo_card(
            user_query=query,
            suggestions=suggestions,
            competitors=competitors,
            mode=mode,
            keyword_stats=keyword_stats or None,
        )

        # Сохраняем карточку для /upload
        if seo_data.get("title") or seo_data.get("description"):
            session["last_card"] = {
                "title": seo_data.get("title", ""),
                "description": seo_data.get("description", ""),
            }

        await gen_msg.delete()
        await _send_result(
            message=callback.message,  # type: ignore[union-attr]
            query=query,
            seo_data=seo_data,
            mode=mode,
            kw_count=len(suggestions),
            comp_count=len(competitors),
        )

    except Exception as e:
        logger.error("Ошибка генерации [%s]: %s", mode, e, exc_info=True)
        try:
            await gen_msg.edit_text(
                f"<b>Ошибка генерации</b>\n\n<code>{e}</code>\n\nПопробуй ещё раз.",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# UI: прогресс-бар во время сбора данных
# ─────────────────────────────────────────────────────────────────────────────

def _progress(step: int, query: str, comp_count: int = 0, kw_count: int = 0) -> str:
    total = 3
    bar = "●" * (step - 1) + "○" * (total - step + 1)
    lines = [f"<code>{bar}</code>  <b>{query}</b>\n"]
    if step == 1:
        lines.append("◌ Ищу конкурентов в WB…")
    elif step == 2:
        lines.append(f"✓ Конкурентов: {comp_count}")
        lines.append("◌ Собираю ключевые слова…")
    elif step >= 3:
        lines.append(f"✓ Конкурентов: {comp_count}")
        lines.append(f"✓ Ключевых слов: {kw_count}")
        lines.append("◌ Получаю частотность WB API…")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# UI: сводка WB + кнопки
# ─────────────────────────────────────────────────────────────────────────────

def _wb_info_block(
    query: str,
    preset: str,
    norm_query: str,
    comp_count: int,
    kw_count: int,
    has_freq: bool,
) -> str:
    preset_display = preset if preset != query else "—"
    norm_display = norm_query if norm_query != query else "—"
    freq_line = "\nЧастотность: ✓ загружена" if has_freq else ""

    comp_status = f"✓ {comp_count} товаров" if comp_count > 0 else "— не найдено"
    kw_status = f"✓ {kw_count} фраз" if kw_count > 0 else "— нет"

    return (
        f"<b>Данные WB собраны</b>\n\n"
        f"Запрос: <code>{query}</code>\n"
        f"Нормализация WB: <code>{norm_display}</code>\n"
        f"Preset WB: <code>{preset_display}</code>\n\n"
        f"Конкуренты в выдаче: {comp_status}\n"
        f"Ключевые слова: {kw_status}"
        f"{freq_line}\n\n"
        f"<b>Что сгенерировать?</b>"
    )


def _generating_msg(mode: GenerateMode, needs_descriptions: bool) -> str:
    labels = {
        "all": "полную карточку",
        "description": "описание",
        "keywords": "ключевые слова",
        "analysis": "анализ конкурентов",
    }
    desc_note = "\n◌ Загружаю описания конкурентов…" if needs_descriptions else ""
    return (
        f"◌ Генерирую {labels[mode]}…"
        f"{desc_note}\n"
        f"◌ Gemini AI обрабатывает данные…"
    )


# ─────────────────────────────────────────────────────────────────────────────
# UI: вывод результата
# ─────────────────────────────────────────────────────────────────────────────

async def _send_result(
    message: Message,
    query: str,
    seo_data: dict[str, str],
    mode: GenerateMode,
    kw_count: int,
    comp_count: int,
) -> None:
    if seo_data.get("raw"):
        await message.answer(seo_data["raw"], reply_markup=_regenerate_keyboard())
        return

    title = seo_data.get("title", "")
    description = seo_data.get("description", "")
    keywords = seo_data.get("keywords", "")
    analysis = seo_data.get("analysis", "")

    # Шапка
    header = f"<b>{query}</b>  ·  {kw_count} ключей  ·  {comp_count} конкурентов\n"

    # Название
    if title:
        tlen = len(title)
        tok = "✓" if tlen <= 60 else "⚠"
        await message.answer(
            f"{header}\n<b>Название</b>  {tok} {tlen}/60\n\n<code>{title}</code>",
            parse_mode="HTML",
        )

    # Описание
    if description:
        dlen = len(description)
        dnote = "коротко" if dlen < 800 else ("✓" if dlen <= 2000 else "⚠ длинно")
        hdr = f"<b>Описание</b>  {dnote}  {dlen} симв.\n\n"
        if len(hdr) + dlen <= 4000:
            await message.answer(hdr + description, parse_mode="HTML")
        else:
            await message.answer(hdr, parse_mode="HTML")
            for i in range(0, dlen, 3800):
                await message.answer(description[i: i + 3800])
                await asyncio.sleep(0.3)

    # Ключевые слова
    if keywords:
        kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
        kw_text = "\n".join(f"· {k}" for k in kw_list)
        await message.answer(
            f"<b>Ключевые слова</b>  {len(kw_list)} шт.\n\n{kw_text}",
            parse_mode="HTML",
        )

    # Анализ конкурентов
    if analysis:
        await message.answer(
            f"<b>Анализ конкурентов</b>\n\n{analysis}",
            parse_mode="HTML",
        )

    # Кнопки для дальнейшей работы
    upload_tip = "\n/upload — загрузить на WB" if is_wb_api_available() else ""
    await message.answer(
        f"<i>Напишите новый запрос или выберите другой раздел{upload_tip}</i>",
        parse_mode="HTML",
        reply_markup=_regenerate_keyboard(),
    )
