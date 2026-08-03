"""
Хэндлер генерации SEO-карточки.

Порядок вызовов (критично для обхода 429 WB):
  1. search_competitors()   — catalog запрос, пока IP «свежий»
  2. get_suggestions()      — suggests с паузой после catalog
  3. enrich_suggestions_from_competitors() — ключи из имён конкурентов
  4. collect_competitors_data() — детали карточек (параллельно, semaphore=2)
  5. generate_seo_card()    — Gemini AI
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
from services.wb_api import (
    collect_competitors_data,
    enrich_suggestions_from_competitors,
    get_suggestions,
    search_competitors,
)
from services.wb_partner_api import (
    get_search_query_stats,
    is_wb_api_available,
    upload_card_to_wb,
)

logger = logging.getLogger(__name__)
router = Router()

# In-memory хранилище последней карточки (user_id → данные)
_last_card: dict[int, dict[str, str]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# FSM состояния
# ─────────────────────────────────────────────────────────────────────────────

class SeoStates(StatesGroup):
    waiting_for_upload_nm = State()
    waiting_for_upload_confirm = State()


# ─────────────────────────────────────────────────────────────────────────────
# /help
# ─────────────────────────────────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    wb_badge = "✦ WB Partner API подключён" if is_wb_api_available() else "◦ WB Partner API не настроен"
    await message.answer(
        "<b>WB SEO Bot</b>\n\n"
        "Напишите название товара — получите готовую SEO-карточку.\n\n"
        "<b>Команды</b>\n"
        "/upload — загрузить карточку на WB\n"
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
            "1. <a href='https://seller.wildberries.ru/supplier-settings/access-to-api'>WB Кабинет → Настройки → Доступ к API</a>\n"
            "2. <code>WB_API_TOKEN=...</code> в файл .env\n"
            "3. Перезапусти бота",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    card = _last_card.get(user_id)
    if not card:
        await message.answer(
            "Нет сохранённой карточки.\n"
            "Сначала сгенерируй SEO — напиши название товара."
        )
        return

    await state.set_state(SeoStates.waiting_for_upload_nm)
    await state.update_data(card=card)
    await message.answer(
        "<b>Загрузка на WB</b>\n\n"
        "Введи артикул WB (nm_id) — числовой идентификатор товара.\n\n"
        "<i>WB кабинет → Товары → Карточки товаров → столбец «Артикул WB»</i>",
        parse_mode="HTML",
    )


@router.message(SeoStates.waiting_for_upload_nm)
async def handle_upload_nm(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Введи числовой артикул WB, например: <code>123456789</code>", parse_mode="HTML")
        return

    nm_id = int(text)
    data = await state.get_data()
    await state.update_data(nm_id=nm_id)
    await state.set_state(SeoStates.waiting_for_upload_confirm)

    title = data["card"].get("title", "")
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
    nm_id: int = data["nm_id"]
    card: dict[str, str] = data["card"]

    wait = await message.answer("Загружаю на WB…")
    success, msg = await upload_card_to_wb(
        nm_id=nm_id,
        title=card.get("title", ""),
        description=card.get("description", ""),
    )
    await wait.delete()
    await message.answer(msg, parse_mode="HTML")
    await state.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Основной хэндлер
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

    use_wb_api = is_wb_api_available()
    status = await message.answer(_status_msg(query, step=1, use_wb_api=use_wb_api))

    try:
        # ── 1. Поиск конкурентов (catalog — первый запрос!) ──────────────────
        nm_ids = await search_competitors(query, limit=settings.COMPETITORS_COUNT)

        await status.edit_text(_status_msg(query, step=2, nm_ids=nm_ids, use_wb_api=use_wb_api))

        # ── 2. Автоподсказки WB ──────────────────────────────────────────────
        suggestions = await get_suggestions(query, count=settings.SUGGESTIONS_COUNT)

        # ── 3. WB Partner API: частотность (опц.) ────────────────────────────
        keyword_stats: dict[str, int] = {}
        if use_wb_api:
            await status.edit_text(_status_msg(query, step=3, nm_ids=nm_ids, suggestions=suggestions, use_wb_api=use_wb_api))
            keyword_stats = await get_search_query_stats(suggestions)

        # ── 4. Детали конкурентов ─────────────────────────────────────────────
        await status.edit_text(_status_msg(
            query, step=4 if use_wb_api else 3,
            nm_ids=nm_ids, suggestions=suggestions, use_wb_api=use_wb_api,
        ))

        competitors = []
        if nm_ids:
            competitors = await collect_competitors_data(query, count=settings.COMPETITORS_COUNT)

        # Обогащаем ключи словами из конкурентов
        suggestions = enrich_suggestions_from_competitors(suggestions, competitors)

        # ── 5. Генерация через Gemini ─────────────────────────────────────────
        last_step = 5 if use_wb_api else 4
        await status.edit_text(_status_msg(
            query, step=last_step,
            nm_ids=nm_ids, suggestions=suggestions,
            competitors=competitors, use_wb_api=use_wb_api,
        ))

        seo_data = await generate_seo_card(
            user_query=query,
            suggestions=suggestions,
            competitors=competitors,
            keyword_stats=keyword_stats or None,
        )

        await status.delete()

        # Сохраняем карточку для /upload
        user_id = message.from_user.id  # type: ignore[union-attr]
        _last_card[user_id] = {
            "title": seo_data.get("title", ""),
            "description": seo_data.get("description", ""),
        }

        await _send_result(
            message=message,
            query=query,
            seo_data=seo_data,
            kw_count=len(suggestions),
            comp_count=len(competitors),
            has_freq=bool(keyword_stats),
        )

    except Exception as e:
        logger.error("Ошибка для запроса '%s': %s", query, e, exc_info=True)
        try:
            await status.edit_text(
                f"<b>Ошибка</b>\n\n<code>{e}</code>\n\nПопробуйте ещё раз.",
                parse_mode="HTML",
            )
        except Exception:
            await message.answer(f"Ошибка: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# UI: статус-сообщение (прогресс)
# ─────────────────────────────────────────────────────────────────────────────

def _status_msg(
    query: str,
    step: int,
    nm_ids: list | None = None,
    suggestions: list | None = None,
    competitors: list | None = None,
    use_wb_api: bool = False,
) -> str:
    total = 5 if use_wb_api else 4
    filled = min(step - 1, total)
    bar = "●" * filled + "○" * (total - filled)

    lines = [f"<code>{bar}</code>  <b>{query}</b>\n"]

    if step > 1:
        found = len(nm_ids) if nm_ids is not None else 0
        icon = "✓" if found > 0 else "—"
        lines.append(f"{icon} Конкуренты: {found} найдено")
    else:
        lines.append("◌ Ищу конкурентов в WB…")

    if step > 2:
        kw = len(suggestions) if suggestions is not None else 0
        lines.append(f"✓ Ключевые слова: {kw} шт.")
    elif step == 2:
        lines.append("◌ Получаю ключевые слова…")

    if use_wb_api:
        if step > 3:
            lines.append("✓ Частотность WB API")
        elif step == 3:
            lines.append("◌ Получаю частотность…")

    freq_step = 4 if use_wb_api else 3
    if step > freq_step:
        comp = len(competitors) if competitors is not None else 0
        lines.append(f"✓ Описания конкурентов: {comp} шт.")
    elif step == freq_step:
        lines.append("◌ Загружаю описания конкурентов…")

    gemini_step = 5 if use_wb_api else 4
    if step == gemini_step:
        lines.append("◌ Генерирую SEO через Gemini…")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# UI: результат SEO-карточки
# ─────────────────────────────────────────────────────────────────────────────

async def _send_result(
    message: Message,
    query: str,
    seo_data: dict[str, str],
    kw_count: int,
    comp_count: int,
    has_freq: bool,
) -> None:
    """Отправляет SEO-карточку четырьмя лаконичными сообщениями."""

    if seo_data.get("raw"):
        await message.answer(seo_data["raw"])
        return

    title = seo_data.get("title", "—")
    description = seo_data.get("description", "—")
    keywords = seo_data.get("keywords", "—")
    analysis = seo_data.get("analysis", "")

    freq_tag = " · частотность WB" if has_freq else ""
    title_len = len(title)
    title_ok = title_len <= 60
    desc_len = len(description)

    # ── Сообщение 1: Название ─────────────────────────────────────────────────
    await message.answer(
        f"<b>{query}</b>  ·  {kw_count} ключей  ·  {comp_count} конкурентов{freq_tag}\n\n"
        f"<b>Название</b>  {'✓' if title_ok else '⚠'} {title_len}/60\n\n"
        f"<code>{title}</code>",
        parse_mode="HTML",
    )

    # ── Сообщение 2: Описание ─────────────────────────────────────────────────
    desc_note = "коротко" if desc_len < 800 else ("✓ норма" if desc_len <= 2000 else "⚠ длинно")
    header = f"<b>Описание</b>  {desc_note}  {desc_len} симв.\n\n"

    if len(header) + desc_len <= 4000:
        await message.answer(header + description, parse_mode="HTML")
    else:
        await message.answer(header, parse_mode="HTML")
        for i in range(0, desc_len, 3800):
            await message.answer(description[i: i + 3800])
            await asyncio.sleep(0.3)

    # ── Сообщение 3: Ключевые слова ───────────────────────────────────────────
    kw_list = [kw.strip() for kw in keywords.split(",") if kw.strip()]
    kw_text = "\n".join(f"· {kw}" for kw in kw_list)
    await message.answer(
        f"<b>Ключевые слова</b>  {len(kw_list)} шт.\n\n{kw_text}",
        parse_mode="HTML",
    )

    # ── Сообщение 4: Анализ конкурентов ──────────────────────────────────────
    upload_tip = "\n/upload — загрузить на WB" if is_wb_api_available() else ""
    footer = f"<i>Напишите следующий товар{upload_tip}</i>"

    if analysis:
        await message.answer(
            f"<b>Анализ конкурентов</b>\n\n{analysis}\n\n{footer}",
            parse_mode="HTML",
        )
    else:
        await message.answer(footer, parse_mode="HTML")
