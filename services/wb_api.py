"""
Сервис для работы с публичным API Wildberries (без авторизации).

Используемые endpoints:
- search.wb.ru  — поиск товаров (топ конкурентов) + автоподсказки (resultset=suggests)
- card.wb.ru    — детальные данные карточки (название, бренд, категория)
- basket-XX.wbbasket.ru — описание товара (card.json)
"""

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

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

_TIMEOUT = httpx.Timeout(20.0)

# Задержка между запросами к WB (секунды) — защита от 429
_REQUEST_DELAY = 0.5
# Сколько раз повторять запрос при 429
_RETRY_COUNT = 3
_RETRY_BACKOFF = 2.0  # множитель задержки


@dataclass
class CompetitorProduct:
    """Данные одного товара-конкурента с WB."""

    nm_id: int
    name: str
    brand: str
    subject_name: str
    description: str
    keywords_from_name: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Запрос с retry при 429
# ─────────────────────────────────────────────────────────────────────────────

async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict | None = None,
) -> httpx.Response | None:
    """
    GET-запрос с автоматическим retry при HTTP 429.
    Возвращает None если все попытки исчерпаны.
    """
    delay = _REQUEST_DELAY
    for attempt in range(1, _RETRY_COUNT + 1):
        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 429:
                wait = delay * (attempt ** _RETRY_BACKOFF)
                logger.warning(
                    "429 от %s, ожидание %.1fs (попытка %d/%d)",
                    url, wait, attempt, _RETRY_COUNT,
                )
                await asyncio.sleep(wait)
                continue
            return resp
        except httpx.TimeoutException:
            logger.warning("Таймаут %s (попытка %d/%d)", url, attempt, _RETRY_COUNT)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.warning("Ошибка запроса %s: %s", url, e)
            return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Автоподсказки
# ─────────────────────────────────────────────────────────────────────────────

async def get_suggestions(query: str, count: int = 20) -> list[str]:
    """
    Возвращает список ключевых слов/фраз из автоподсказок поиска WB.
    Использует resultset=suggests (актуальный рабочий endpoint).
    """
    suggestions: list[str] = [query]  # сам запрос всегда первый

    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT) as client:
        resp = await _get_with_retry(
            client,
            "https://search.wb.ru/exactmatch/ru/common/v9/search",
            params={
                "appType": 1,
                "curr": "rub",
                "dest": -1257786,
                "lang": "ru",
                "query": query,
                "resultset": "suggests",
                "suppressSpellcheck": "false",
            },
        )

        if resp is None or resp.status_code != 200:
            logger.warning(
                "Не удалось получить подсказки WB (status=%s)",
                resp.status_code if resp else "no_response",
            )
            return suggestions

        try:
            data = resp.json()
        except Exception:
            logger.warning("Невалидный JSON от WB suggests")
            return suggestions

        # Парсим подсказки — WB возвращает их в разных форматах
        for item in data.get("hints", []):
            text = _extract_hint_text(item)
            if text and text not in suggestions:
                suggestions.append(text)

        # Дополнительно — ключи из metadata
        for item in data.get("shardList", []):
            text = _extract_hint_text(item)
            if text and text not in suggestions:
                suggestions.append(text)

    logger.info("Получено %d подсказок для '%s'", len(suggestions), query)
    return suggestions[:count]


def _extract_hint_text(item: dict | str) -> str:
    """Извлекает текст подсказки из разных форматов ответа WB."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return (
            item.get("hint")
            or item.get("name")
            or item.get("text")
            or item.get("query")
            or ""
        ).strip()
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Поиск конкурентов
# ─────────────────────────────────────────────────────────────────────────────

async def search_competitors(query: str, limit: int = 10) -> list[int]:
    """
    Возвращает список nm_id (артикулов) топ-товаров WB по поисковому запросу.
    """
    nm_ids: list[int] = []

    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT) as client:
        resp = await _get_with_retry(
            client,
            "https://search.wb.ru/exactmatch/ru/common/v9/search",
            params={
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
            },
        )

        if resp is None or resp.status_code != 200:
            logger.error(
                "Не удалось получить конкурентов WB (status=%s)",
                resp.status_code if resp else "no_response",
            )
            return nm_ids

        try:
            data = resp.json()
        except Exception:
            logger.error("Невалидный JSON от WB search")
            return nm_ids

        products = data.get("data", {}).get("products", [])
        for product in products[:limit]:
            nm_id = product.get("id")
            if nm_id:
                nm_ids.append(int(nm_id))

    logger.info("Найдено %d конкурентов для '%s'", len(nm_ids), query)
    return nm_ids


# ─────────────────────────────────────────────────────────────────────────────
# Детальные данные товара
# ─────────────────────────────────────────────────────────────────────────────

async def get_product_details(nm_id: int) -> CompetitorProduct | None:
    """
    Получает детальную информацию о товаре по nm_id:
    - Название, бренд, категория — из card.wb.ru
    - Описание — из basket-XX.wbbasket.ru/vol.../card.json
    """
    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT) as client:
        # ── 1. Основные данные: название, бренд, категория ────────────────
        resp_detail = await _get_with_retry(
            client,
            "https://card.wb.ru/cards/v2/detail",
            params={"appType": 1, "curr": "rub", "nm": nm_id, "dest": -1257786},
        )

        if resp_detail is None or resp_detail.status_code != 200:
            logger.warning("Нет данных карточки для nm_id=%d", nm_id)
            return None

        try:
            catalog_data = resp_detail.json()
        except Exception:
            return None

        products = catalog_data.get("data", {}).get("products", [])
        if not products:
            return None

        p = products[0]
        name: str = p.get("name", "")
        brand: str = p.get("brand", "")
        subject_name: str = p.get("subjectName", "")

        await asyncio.sleep(0.3)

        # ── 2. Описание из basket ─────────────────────────────────────────
        description = await _fetch_card_description(client, nm_id)

        return CompetitorProduct(
            nm_id=nm_id,
            name=name,
            brand=brand,
            subject_name=subject_name,
            description=description,
            keywords_from_name=_extract_keywords(name),
        )


async def _fetch_card_description(client: httpx.AsyncClient, nm_id: int) -> str:
    """
    Получает описание товара из basket-хоста WB.
    Правильный URL: basket-XX.wbbasket.ru/vol{vol}/part{part}/{nm_id}/info/ru/card.json
    """
    vol = nm_id // 100000
    part = nm_id // 1000
    basket = _resolve_basket_num(vol)
    url = (
        f"https://basket-{basket}.wbbasket.ru"
        f"/vol{vol}/part{part}/{nm_id}/info/ru/card.json"
    )

    try:
        resp = await _get_with_retry(client, url)
        if resp is None or resp.status_code != 200:
            return ""
        data = resp.json()
        return data.get("description", "")
    except Exception as e:
        logger.debug("Нет описания для nm_id=%d: %s", nm_id, e)
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Сбор данных конкурентов (оркестратор)
# ─────────────────────────────────────────────────────────────────────────────

async def collect_competitors_data(
    query: str, count: int = 10
) -> list[CompetitorProduct]:
    """
    Собирает данные конкурентов: поиск → детали каждого товара параллельно.
    Параллельные запросы ограничены семафором чтобы не получать 429.
    """
    nm_ids = await search_competitors(query, limit=count)
    if not nm_ids:
        logger.warning("Конкуренты не найдены для запроса: %s", query)
        return []

    # Семафор: не более 3 одновременных запросов к WB
    semaphore = asyncio.Semaphore(3)

    async def fetch_with_limit(nm_id: int) -> CompetitorProduct | None:
        async with semaphore:
            result = await get_product_details(nm_id)
            await asyncio.sleep(0.3)  # вежливая задержка
            return result

    tasks = [fetch_with_limit(nm_id) for nm_id in nm_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    competitors: list[CompetitorProduct] = []
    for result in results:
        if isinstance(result, CompetitorProduct):
            competitors.append(result)
        elif isinstance(result, Exception):
            logger.warning("Ошибка при сборе данных конкурента: %s", result)

    logger.info("Собрано %d конкурентов", len(competitors))
    return competitors


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_basket_num(vol: int) -> str:
    """Определяет номер basket-хоста по vol (nm_id // 100000)."""
    thresholds = [
        (143, "01"), (287, "02"), (431, "03"), (719, "04"),
        (1007, "05"), (1061, "06"), (1115, "07"), (1169, "08"),
        (1313, "09"), (1601, "10"), (1655, "11"), (1919, "12"),
        (2045, "13"), (2189, "14"), (2405, "15"), (2621, "16"),
        (2837, "17"),
    ]
    for threshold, num in thresholds:
        if vol <= threshold:
            return num
    return "18"


def _extract_keywords(text: str) -> list[str]:
    """Извлекает значимые слова из названия товара."""
    stop_words = {
        "и", "в", "на", "для", "с", "по", "из", "от", "к", "у", "о",
        "не", "это", "как", "но", "а", "же", "так", "то", "что", "если",
        "все", "при", "за", "об", "или", "до", "без", "уже", "мм", "см",
        "кг", "шт", "г", "л", "мл", "пк", "цвет", "размер",
    }
    words = text.lower().split()
    return [
        w.strip(".,;:!?\"'()[]") for w in words
        if len(w) > 3 and w.strip(".,;:!?\"'()[]") not in stop_words
    ]
