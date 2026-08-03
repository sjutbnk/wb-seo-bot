from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Telegram ──────────────────────────────────────────────────────────────
    BOT_TOKEN: str

    # ── Gemini AI ─────────────────────────────────────────────────────────────
    GEMINI_API_KEY: str
    # Первая модель в цепочке fallback (по загруженности серверов):
    # gemini-3.1-flash → gemini-3.1-flash-lite → gemini-3.5-flash
    GEMINI_MODEL: str = "gemini-3.1-flash"

    # ── WB Partner API (опционально) ──────────────────────────────────────────
    # Токен из личного кабинета WB: https://seller.wildberries.ru/supplier-settings/access-to-api
    # Если не задан — партнёрские функции (частотность, загрузка карточки) отключены.
    WB_API_TOKEN: str = ""

    # ── Параметры сбора данных ────────────────────────────────────────────────
    # Количество конкурентов для анализа (топ N товаров из поиска WB)
    COMPETITORS_COUNT: int = 10
    # Количество ключевых слов из автоподсказок WB
    SUGGESTIONS_COUNT: int = 20

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
