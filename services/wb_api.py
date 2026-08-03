"""
Сервис для работы с публичным API Wildberries.

Стратегия запросов (избегаем 429):
1. Catalog-поиск идёт ПЕРВЫМ — пока IP «свежий»
2. Suggests идёт ВТОРЫМ с паузой
3. Ключи дополняются из имён/категорий конкурентов
4. Retry с экспоненциальным backoff при 429
5. Semaphore на параллельные запросы к деталям карточек
"""

import asyncio
import logging
import random
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Константы
# ─────────────────────────────────────────────────────────────────────────────

_TIMEOUT = httpx.Timeout(20.0)
_RETRY_COUNT = 3
_RETRY_BASE_DELAY = 2.0  # секунды

# Ротация User-Agent для снижения вероятности блока
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

_WB_DEST = -1257786  # Москва


def _make_headers() -> dict[str, str]:
    """Случайный User-Agent при каждом запросе."""
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.wildberries.ru/",
        "Origin": "https://www.wildberries.ru",
        "Connection": "keep-alive",
        "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Модели данных
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CompetitorProduct:
    """Данные товара-конкурента из WB."""
    nm_id: int
    name: str
    brand: str
    subject_name: str
    description: str
    keywords_from_name: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP с retry
# ─────────────────────────────────────────────────────────────────────────────

async def _get(
    client: httpx.AsyncClient,
    url: str,
    params: dict | None = None,
) -> httpx.Response | None:
    """
    GET с retry при 429. Экспоненциальный backoff + jitter.
    """
    for attempt in range(1, _RETRY_COUNT + 1):
        try:
            resp = await client.get(url, params=params, headers=_make_headers())
            if resp.status_code == 429:
                delay = _RETRY_BASE_DELAY ** attempt + random.uniform(0.5, 1.5)
                logger.warning("429 от WB [%s], ожидание %.1fs (попытка %d/%d)", url, delay, attempt, _RETRY_COUNT)
                await asyncio.sleep(delay)
                continue
            return resp
        except httpx.TimeoutException:
            logger.warning("Таймаут %s (попытка %d/%d)", url, attempt, _RETRY_COUNT)
            await asyncio.sleep(_RETRY_BASE_DELAY * attempt)
        except Exception as e:
            logger.warning("Ошибка запроса %s: %s", url, e)
            return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Поиск конкурентов (ПРИОРИТЕТ — запускается первым)
# ─────────────────────────────────────────────────────────────────────────────

async def search_competitors(query: str, limit: int = 10) -> list[int]:
    """
    Топ nm_id из поиска WB по запросу.
    Запускается ПЕРВЫМ, пока IP ещё не заблокирован.
    """
    nm_ids: list[int] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await _get(
            client,
            "https://search.wb.ru/exactmatch/ru/common/v9/search",
            params={
                "appType": 1,
                "curr": "rub",
                "dest": _WB_DEST,
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
            logger.error("Поиск конкурентов: status=%s", resp.status_code if resp else "none")
            return nm_ids

        try:
            data = resp.json()
            for p in data.get("data", {}).get("products", [])[:limit]:
                if nm_id := p.get("id"):
                    nm_ids.append(int(nm_id))
        except Exception as e:
            logger.error("Ошибка парсинга поиска WB: %s", e)

    logger.info("Найдено конкурентов: %d", len(nm_ids))
    return nm_ids


# ─────────────────────────────────────────────────────────────────────────────
# Автоподсказки (запускается ПОСЛЕ поиска конкурентов)
# ─────────────────────────────────────────────────────────────────────────────

async def get_suggestions(query: str, count: int = 20) -> list[str]:
    """
    Ключевые слова из WB suggests.
    resultset=suggests возвращает нормализованный запрос + metaданные.
    Дополнительно генерируем варианты запроса вручную.
    """
    suggestions: list[str] = [query]

    await asyncio.sleep(random.uniform(1.5, 3.0))  # пауза после catalog-запроса

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await _get(
            client,
            "https://search.wb.ru/exactmatch/ru/common/v9/search",
            params={
                "appType": 1,
                "curr": "rub",
                "dest": _WB_DEST,
                "lang": "ru",
                "query": query,
                "resultset": "suggests",
                "suppressSpellcheck": "false",
            },
        )

        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                # normQuery — нормализованный вариант запроса от WB
                norm = data.get("normQuery", "")
                if norm and norm != query and norm not in suggestions:
                    suggestions.append(norm)
                # name — оригинальный запрос (обычно совпадает)
                name = data.get("name", "")
                if name and name not in suggestions:
                    suggestions.append(name)
            except Exception as e:
                logger.warning("Ошибка парсинга suggests: %s", e)

    # Дополняем вариациями запроса (частые паттерны WB)
    words = query.lower().split()
    if len(words) >= 2:
        # Перестановка слов
        for i in range(len(words)):
            variant = " ".join(words[i:] + words[:i])
            if variant != query and variant not in suggestions:
                suggestions.append(variant)
        # Добавляем категорийные уточнения
        for suffix in ["купить", "цена", "недорого", "отзывы", "характеристики"]:
            var = f"{query} {suffix}"
            if var not in suggestions:
                suggestions.append(var)

    return suggestions[:count]


# ─────────────────────────────────────────────────────────────────────────────
# Детали товара
# ─────────────────────────────────────────────────────────────────────────────

async def get_product_details(nm_id: int) -> CompetitorProduct | None:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        # Основные данные
        resp = await _get(
            client,
            "https://card.wb.ru/cards/v2/detail",
            params={"appType": 1, "curr": "rub", "nm": nm_id, "dest": _WB_DEST},
        )
        if resp is None or resp.status_code != 200:
            return None

        try:
            products = resp.json().get("data", {}).get("products", [])
        except Exception:
            return None

        if not products:
            return None

        p = products[0]
        name: str = p.get("name", "")
        brand: str = p.get("brand", "")
        subject_name: str = p.get("subjectName", "")

        await asyncio.sleep(random.uniform(0.2, 0.5))

        description = await _fetch_description(client, nm_id)

        return CompetitorProduct(
            nm_id=nm_id,
            name=name,
            brand=brand,
            subject_name=subject_name,
            description=description,
            keywords_from_name=_extract_keywords(name),
        )


async def _fetch_description(client: httpx.AsyncClient, nm_id: int) -> str:
    vol = nm_id // 100000
    part = nm_id // 1000
    basket = _resolve_basket(vol)
    url = f"https://basket-{basket}.wbbasket.ru/vol{vol}/part{part}/{nm_id}/info/ru/card.json"
    try:
        resp = await _get(client, url)
        if resp and resp.status_code == 200:
            return resp.json().get("description", "")
    except Exception as e:
        logger.debug("Нет описания nm=%d: %s", nm_id, e)
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Оркестратор: порядок важен
# ─────────────────────────────────────────────────────────────────────────────

async def collect_competitors_data(query: str, count: int = 10) -> list[CompetitorProduct]:
    """
    Параллельный сбор данных конкурентов.
    Semaphore ограничивает нагрузку на WB.
    """
    nm_ids = await search_competitors(query, limit=count)
    if not nm_ids:
        return []

    semaphore = asyncio.Semaphore(2)  # не более 2 одновременных запросов

    async def fetch(nm_id: int) -> CompetitorProduct | None:
        async with semaphore:
            result = await get_product_details(nm_id)
            await asyncio.sleep(random.uniform(0.3, 0.8))
            return result

    results = await asyncio.gather(*[fetch(nm) for nm in nm_ids], return_exceptions=True)

    competitors = [r for r in results if isinstance(r, CompetitorProduct)]
    logger.info("Собрано данных конкурентов: %d/%d", len(competitors), len(nm_ids))
    return competitors


def enrich_suggestions_from_competitors(
    suggestions: list[str],
    competitors: list[CompetitorProduct],
    count: int = 20,
) -> list[str]:
    """
    Дополняет список ключевых слов словами из имён конкурентов.
    Вызывается в handler ПОСЛЕ получения конкурентов.
    """
    enriched = list(suggestions)
    seen = {s.lower() for s in enriched}

    for c in competitors[:8]:
        for kw in c.keywords_from_name:
            if kw.lower() not in seen and len(kw) > 3:
                enriched.append(kw)
                seen.add(kw.lower())
        # Добавляем subject как ключ (категория товара)
        subj = c.subject_name.strip().lower()
        if subj and subj not in seen:
            enriched.append(c.subject_name)
            seen.add(subj)

    return enriched[:count]


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_basket(vol: int) -> str:
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
    stop = {
        "и","в","на","для","с","по","из","от","к","у","о","не","это","как",
        "но","а","же","так","то","что","если","все","при","за","об","или",
        "до","без","уже","мм","см","кг","шт","г","л","мл","пк","цвет","размер",
    }
    return [
        w.strip(".,;:!?\"'()[]") for w in text.lower().split()
        if len(w) > 3 and w.strip(".,;:!?\"'()[]") not in stop
    ]
