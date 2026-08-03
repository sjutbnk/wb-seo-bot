"""
Хэндлер /start — приветственное сообщение.
"""

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

router = Router()

_EXAMPLE_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="▶ Попробовать: робот мойщик окон", callback_data="example:робот мойщик окон")],
])


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "<b>WB SEO Bot</b>\n\n"
        "Создаю SEO-карточки для Wildberries:\n"
        "— Реальные ключи из поиска WB\n"
        "— Анализ топ-10 конкурентов\n"
        "— Продающее описание через Gemini AI\n"
        "— Загрузка карточки напрямую на WB\n\n"
        "<b>Как использовать</b>\n"
        "Напишите название или тип товара:\n"
        "<i>кроссовки мужские летние</i>\n"
        "<i>робот мойщик окон с распылителем</i>\n\n"
        "После сбора данных выберите что сгенерировать:\n"
        "📝 Описание · 🔑 Ключи · 📊 Анализ · ✅ Всё\n\n"
        "/upload — загрузить карточку на WB\n"
        "/help — справка",
        parse_mode="HTML",
    )
