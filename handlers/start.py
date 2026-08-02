"""
Хэндлер команды /start — приветственное сообщение.
"""

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 <b>Привет! Я SEO-бот для Wildberries</b>\n\n"
        "Я помогу создать SEO-оптимизированную карточку товара:\n"
        "• Подберу ключевые слова из реального поиска WB\n"
        "• Проанализирую топ конкурентов в выдаче\n"
        "• Сгенерирую название, описание и список ключей\n\n"
        "📝 <b>Как использовать:</b>\n"
        "Просто напиши название или тип товара, например:\n"
        "<i>кроссовки мужские летние</i>\n"
        "<i>платье вечернее с открытой спиной</i>\n"
        "<i>наушники беспроводные для спорта</i>\n\n"
        "⏱ Анализ занимает 20-40 секунд.",
        parse_mode="HTML",
    )
