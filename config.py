from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    BOT_TOKEN: str
    GEMINI_API_KEY: str
    # Количество конкурентов для анализа (топ N товаров из поиска WB)
    COMPETITORS_COUNT: int = 10
    # Количество ключевых слов из автоподсказок WB
    SUGGESTIONS_COUNT: int = 20

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
