"""
Сервис для работы с публичным API Wildberries.

Используемые endpoints (без авторизации):
- search.wb.ru — автоподсказки поиска
- search.wb.ru — поиск товаров (топ конкурентов)
- card.wb.ru   — карточки товаров (описание, характеристики)
"""

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Заголовки для обхода базовой защиты WB
# ──────────────────────────────────────────────
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": "https://www.wildberries.ru/",
    "Origin": "https://www.wildberries.ru",
}

_TIMEOUT = httpx.Timeout(15.0)


@dataclass
class CompetitorProduct:
    """Данные одного товара-конкурента с WB."""

    nm_id: int
    name: str
    brand: str
    subject_name: str  # категория
    description: str
    keywords_from_name: list[str] = field(default_factory=list)


async def get_suggestions(query: str, count: int = 20) -> list[str]:
    """
    Получает список автоподсказок поиска WB для заданного запроса.
    Возвращает до `count` уникальных ключевых слов/фраз.
    """
    url = "https://search.wb.ru/exactmatch/ru/common/v9/search"
    params = {
        "appType": 1,
        "curr": "rub",
        "dest": -1257786,
        "lang": "ru",
        "query": query,
        "resultset": "catalog",
        "sort": "popular",
        "spp": 30,
        "suppressSpellcheck": "false",
    }

    suggestions: list[str] = []

    # Endpoint автоподсказок
    suggest_url = "https://search.wb.ru/suggests/api/v4/search"
    suggest_params = {
        "appType": 1,
        "lang": "ru",
        "locale": "ru",
        "query": query,
    }

    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT) as client:
        try:
            resp = await client.get(suggest_url, params=suggest_params)
            resp.raise_for_status()
            data = resp.json()

            # Парсим подсказки из ответа
            for item in data.get("hints", [])[:count]:
                if isinstance(item, dict):
                    text = item.get("hint") or item.get("text") or item.get("name", "")
                elif isinstance(item, str):
                    text = item
                else:
                    continue
                if text and text not in suggestions:
                    suggestions.append(text.strip())

        except Exception as e:
            logger.warning("Ошибка получения подсказок WB: %s", e)

    # Если подсказок мало — добавляем вариации запроса
    if len(suggestions) < 5:
        suggestions.insert(0, query)

    return suggestions[:count]


async def search_competitors(query: str, limit: int = 10) -> list[int]:
    """
    Ищет топ товаров WB по запросу.
    Возвращает список nm_id (артикулов) конкурентов.
    """
    url = "https://search.wb.ru/exactmatch/ru/common/v9/search"
    params = {
        "appType": 1,
        "curr": "rub",
        "dest": -1257786,
        "lang": "ru",
        "query": query,
        "resultset": "catalog",
        "sort": "popular",
        "spp": 30,
        "suppressSpellcheck": "false",
        "page": 1,
    }

    nm_ids: list[int] = []

    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT) as client:
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

            products = data.get("data", {}).get("products", [])
            for product in products[:limit]:
                nm_id = product.get("id")
                if nm_id:
                    nm_ids.append(int(nm_id))

        except Exception as e:
            logger.error("Ошибка поиска конкурентов WB: %s", e)

    return nm_ids


async def get_product_details(nm_id: int) -> CompetitorProduct | None:
    """
    Получает детальную информацию о товаре по nm_id.
    Используется card.wb.ru для описания и характеристик.
    """
    # Основные данные карточки
    basket_host = _resolve_basket_host(nm_id)
    card_url = f"https://{basket_host}/info/ru/card.json"

    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT) as client:
        try:
            # Получаем описание с card.wb.ru
            resp = await client.get(card_url)
            resp.raise_for_status()
            card_data = resp.json()

            description = card_data.get("description", "")

            # Данные о товаре из поиска (название, бренд, категория)
            catalog_url = "https://card.wb.ru/cards/v2/detail"
            catalog_params = {"appType": 1, "curr": "rub", "nm": nm_id}
            resp2 = await client.get(catalog_url, params=catalog_params)
            resp2.raise_for_status()
            catalog_data = resp2.json()

            products = catalog_data.get("data", {}).get("products", [])
            if not products:
                return None

            p = products[0]
            name = p.get("name", "")
            brand = p.get("brand", "")
            subject_name = p.get("subjectName", "")

            return CompetitorProduct(
                nm_id=nm_id,
                name=name,
                brand=brand,
                subject_name=subject_name,
                description=description,
                keywords_from_name=_extract_keywords(name),
            )

        except Exception as e:
            logger.warning("Ошибка получения данных товара %d: %s", nm_id, e)
            return None


async def collect_competitors_data(
    query: str, count: int = 10
) -> list[CompetitorProduct]:
    """
    Собирает данные о конкурентах: поиск + детали каждого товара параллельно.
    """
    nm_ids = await search_competitors(query, limit=count)
    if not nm_ids:
        logger.warning("Конкуренты не найдены для запроса: %s", query)
        return []

    tasks = [get_product_details(nm_id) for nm_id in nm_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    competitors: list[CompetitorProduct] = []
    for result in results:
        if isinstance(result, CompetitorProduct):
            competitors.append(result)
        elif isinstance(result, Exception):
            logger.warning("Ошибка при сборе данных конкурента: %s", result)

    return competitors


# ──────────────────────────────────────────────
# Вспомогательные функции
# ──────────────────────────────────────────────

def _resolve_basket_host(nm_id: int) -> str:
    """Определяет хост корзины по nm_id (логика WB)."""
    vol = nm_id // 100000
    part = nm_id // 1000

    if vol <= 143:
        basket = "01"
    elif vol <= 287:
        basket = "02"
    elif vol <= 431:
        basket = "03"
    elif vol <= 719:
        basket = "04"
    elif vol <= 1007:
        basket = "05"
    elif vol <= 1061:
        basket = "06"
    elif vol <= 1115:
        basket = "07"
    elif vol <= 1169:
        basket = "08"
    elif vol <= 1313:
        basket = "09"
    elif vol <= 1601:
        basket = "10"
    elif vol <= 1655:
        basket = "11"
    elif vol <= 1919:
        basket = "12"
    elif vol <= 2045:
        basket = "13"
    elif vol <= 2189:
        basket = "14"
    elif vol <= 2405:
        basket = "15"
    elif vol <= 2621:
        basket = "16"
    elif vol <= 2837:
        basket = "17"
    else:
        basket = "18"

    return f"basket-{basket}.wbbasket.ru"


def _extract_keywords(text: str) -> list[str]:
    """Извлекает значимые слова из названия товара (простая токенизация)."""
    stop_words = {
        "и", "в", "на", "для", "с", "по", "из", "от", "к", "у", "о",
        "не", "это", "как", "но", "а", "же", "так", "то", "что", "если",
        "все", "при", "за", "об", "или", "до", "без", "уже", "мм", "см",
        "кг", "шт", "г", "л", "мл", "пк", "цвет", "размер",
    }
    words = text.lower().split()
    return [
        w.strip(".,;:!?\"'()[]") for w in words
        if len(w) > 3 and w not in stop_words
    ]
