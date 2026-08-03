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
        "Создаю SEO-оптимизированные карточки товаров:\n"
        "• Ключевые слова из реального поиска WB\n"
        "• Анализ топ конкурентов в выдаче\n"
        "• Продающее описание + список ключей через Gemini AI\n"
        "• Прямая загрузка карточки на WB (нужен токен продавца)\n\n"
        "📝 <b>Как использовать:</b>\n"
        "Просто напиши название или тип товара:\n"
        "<i>кроссовки мужские летние</i>\n"
        "<i>платье вечернее с открытой спиной</i>\n\n"
        "⚙️ <b>Команды:</b>\n"
        "/upload — загрузить карточку на WB\n"
        "/help — справка и статус WB API\n\n"
        "⏱ Анализ занимает ~30-60 секунд.",
        parse_mode="HTML",
    )

