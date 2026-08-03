"""
WB публичный API (без авторизации).

Стратегия обхода 429:
- appType ротация (web=1, android=64, ios=4)
- Рандомный UA из пула
- suggests → получаем preset → catalog с preset (как браузер WB)
- Каждый запрос через отдельный AsyncClient (сброс TCP-сессии)
- Exponential backoff с jitter
- Базовые данные конкурентов берём прямо из catalog-ответа (без доп. запросов)
- Описания загружаем только по запросу пользователя
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(20.0)
_WB_DEST = -1257786

# Кэш результатов поиска: {query → (timestamp, data)}
# TTL 30 минут — WB выдача почти не меняется за это время
_SEARCH_CACHE: dict[str, tuple[float, list]] = {}
_CACHE_TTL = 1800  # 30 минут

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

# Android (appType=64) работает для catalog без Referer, возвращает products на верхнем уровне
# Web (appType=1) работает для suggests и basket
_CATALOG_APP_TYPE = 64
_SUGGEST_APP_TYPE = 1


def _headers(app_type: int = 1) -> dict[str, str]:
    if app_type == 64:
        # Android клиент — возвращает products на верхнем уровне, не требует Referer
        return {
            "User-Agent": "WildBerries/9.3.0 (Android; sdk_gphone64_x86_64; API 33)",
            "Accept": "application/json",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Accept-Encoding": "identity",
        }
    if app_type == 4:
        return {
            "User-Agent": "WB/22.0 CFNetwork/1496.0.7 Darwin/23.5.0",
            "Accept": "application/json",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Accept-Encoding": "identity",
        }
    # Web
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
        "Referer": "https://www.wildberries.ru/",
        "Origin": "https://www.wildberries.ru",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }


_SEARCH_URL = "https://search.wb.ru/exactmatch/ru/common/v9/search"


# ─────────────────────────────────────────────────────────────────────────────
# Модели данных
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CompetitorBasic:
    """Данные из catalog-ответа — без дополнительных запросов."""
    nm_id: int
    name: str
    brand: str
    subject_name: str
    keywords_from_name: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "nm_id": self.nm_id,
            "name": self.name,
            "brand": self.brand,
            "subject_name": self.subject_name,
            "keywords_from_name": self.keywords_from_name,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CompetitorBasic":
        return cls(**d)


@dataclass
class CompetitorProduct(CompetitorBasic):
    """Полные данные включая описание из basket."""
    description: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helper
# ─────────────────────────────────────────────────────────────────────────────

async def _get(url: str, params: dict, app_type: int = _SUGGEST_APP_TYPE) -> httpx.Response | None:
    """
    GET с retry. Каждая попытка — новый AsyncClient (сброс TCP-сессии).
    app_type=1 (web) для suggests и basket
    app_type=64 (android) для catalog — возвращает products на верхнем уровне
    """
    fallback_types = [app_type] + [t for t in [_CATALOG_APP_TYPE, _SUGGEST_APP_TYPE] if t != app_type]

    for attempt, atype in enumerate(fallback_types, start=1):
        try:
            p = dict(params)
            p["appType"] = atype

            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, params=p, headers=_headers(atype))

            if resp.status_code == 200 and resp.content:
                return resp

            if resp.status_code == 429:
                delay = (2.0 ** attempt) + random.uniform(1.0, 3.0)
                logger.warning("429 WB [appType=%d], ждём %.1fs", atype, delay)
                await asyncio.sleep(delay)
                continue

            logger.warning("WB status=%d [appType=%d]", resp.status_code, atype)

        except httpx.TimeoutException:
            logger.warning("Таймаут %s (appType=%d)", url, atype)
            await asyncio.sleep(2.0 * attempt)
        except Exception as e:
            logger.warning("HTTP error %s: %s", url, e)
            return None

    logger.error("Все попытки исчерпаны для %s", url)
    return None




# ─────────────────────────────────────────────────────────────────────────────
# Suggests → получаем preset и normQuery
# ─────────────────────────────────────────────────────────────────────────────

async def get_suggests_info(query: str) -> tuple[str, str]:
    """
    Возвращает (preset_query, norm_query).
    preset_query используется для catalog-запроса (как браузер WB).
    """
    resp = await _get(
        _SEARCH_URL,
        params={
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
            preset = data.get("query", "")   # e.g. "preset=500051093"
            norm = data.get("normQuery", "")  # e.g. "робот мойщик окон"
            logger.info("Suggests OK: preset=%r normQuery=%r", preset, norm)
            return preset or query, norm or query
        except Exception as e:
            logger.warning("Ошибка парсинга suggests: %s", e)

    return query, query


# ─────────────────────────────────────────────────────────────────────────────
# Поиск конкурентов (быстрый, только базовые данные)
# ─────────────────────────────────────────────────────────────────────────────

async def search_competitors_basic(
    query: str, limit: int = 10
) -> tuple[list[CompetitorBasic], str, str]:
    """
    Возвращает (competitors_basic, preset_query, norm_query).

    Алгоритм:
    1. Кэш: если запрос уже делали — вернуть кэш
    2. suggests (appType=1) → normQuery
    3. catalog (appType=64) с текстовым запросом → 100 продуктов
    """
    cache_key = query.lower().strip()
    now = time.time()
    if cache_key in _SEARCH_CACHE:
        ts, cached = _SEARCH_CACHE[cache_key]
        if now - ts < _CACHE_TTL:
            logger.info("Кэш: %d конкурентов для %r", len(cached), query)
            return [CompetitorBasic.from_dict(d) for d in cached], query, query

    # Шаг 1: suggests (appType=1)
    preset_query, norm_query = await get_suggests_info(query)
    await asyncio.sleep(random.uniform(2.0, 3.5))

    # Шаг 2: catalog с appType=64 (подтверждено: возвращает products на верхнем уровне)
    resp = await _get(
        _SEARCH_URL,
        params={
            "curr": "rub",
            "dest": _WB_DEST,
            "lang": "ru",
            "query": query,  # текстовый запрос — appType=64 не нужен preset
            "resultset": "catalog",
            "sort": "popular",
            "spp": 30,
            "suppressSpellcheck": "false",
            "page": 1,
        },
        app_type=_CATALOG_APP_TYPE,  # Android: products на верхнем уровне
    )

    competitors: list[CompetitorBasic] = []
    if resp and resp.status_code == 200:
        try:
            d = resp.json()
            # appType=64: products на верхнем уровне d['products']
            # appType=1:  products в d['data']['products']
            products = d.get("products") or d.get("data", {}).get("products", [])
            for p in products[:limit]:
                nm_id = p.get("id")
                if not nm_id:
                    continue
                name = p.get("name", "")
                subject = p.get("subjectName") or p.get("entity", "")
                competitors.append(
                    CompetitorBasic(
                        nm_id=int(nm_id),
                        name=name,
                        brand=p.get("brand", ""),
                        subject_name=subject,
                        keywords_from_name=_extract_keywords(name),
                    )
                )
        except Exception as e:
            logger.error("Ошибка парсинга catalog: %s", e)

    # Кэшируем результат
    if competitors:
        _SEARCH_CACHE[cache_key] = (now, [c.to_dict() for c in competitors])

    logger.info("Конкурентов найдено: %d для %r", len(competitors), query)
    return competitors, preset_query, norm_query



# ─────────────────────────────────────────────────────────────────────────────
# Загрузка описаний (по запросу, когда нужны для генерации)
# ─────────────────────────────────────────────────────────────────────────────

async def load_descriptions(
    competitors_basic: list[CompetitorBasic],
) -> list[CompetitorProduct]:
    """
    Загружает описания из basket для каждого конкурента.
    Семафор = 2 чтобы не перегружать WB.
    """
    semaphore = asyncio.Semaphore(2)

    async def fetch_one(c: CompetitorBasic) -> CompetitorProduct:
        async with semaphore:
            desc = await _fetch_description(c.nm_id)
            await asyncio.sleep(random.uniform(0.4, 0.9))
            return CompetitorProduct(
                nm_id=c.nm_id,
                name=c.name,
                brand=c.brand,
                subject_name=c.subject_name,
                keywords_from_name=c.keywords_from_name,
                description=desc,
            )

    results = await asyncio.gather(*[fetch_one(c) for c in competitors_basic], return_exceptions=True)
    full = [r for r in results if isinstance(r, CompetitorProduct)]
    logger.info("Описаний загружено: %d/%d", len(full), len(competitors_basic))
    return full


async def _fetch_description(nm_id: int) -> str:
    vol = nm_id // 100000
    part = nm_id // 1000
    basket = _resolve_basket(vol)
    url = f"https://basket-{basket}.wbbasket.ru/vol{vol}/part{part}/{nm_id}/info/ru/card.json"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers=_headers())
        if resp and resp.status_code == 200:
            return resp.json().get("description", "")
    except Exception as e:
        logger.debug("Нет описания nm=%d: %s", nm_id, e)
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Ключевые слова
# ─────────────────────────────────────────────────────────────────────────────

def build_suggestions(
    query: str,
    norm_query: str,
    competitors: list[CompetitorBasic],
    count: int = 20,
) -> list[str]:
    """
    Строит список ключевых слов из:
    - оригинального запроса и normQuery от WB
    - слов из названий конкурентов
    - категорий конкурентов
    """
    seen: set[str] = set()
    result: list[str] = []

    def add(kw: str) -> None:
        kl = kw.strip().lower()
        if kl and kl not in seen and len(kl) > 2:
            seen.add(kl)
            result.append(kw.strip())

    add(query)
    add(norm_query)

    # Категории конкурентов
    for c in competitors:
        add(c.subject_name)

    # Слова из названий конкурентов
    all_keywords: list[str] = []
    for c in competitors[:10]:
        all_keywords.extend(c.keywords_from_name)

    # Сортируем по частоте (чаще встречается = более ВЧ)
    kw_freq: dict[str, int] = {}
    for kw in all_keywords:
        kw_freq[kw] = kw_freq.get(kw, 0) + 1
    for kw, _ in sorted(kw_freq.items(), key=lambda x: x[1], reverse=True):
        add(kw)

    # Частые WB-паттерны для запроса
    words = query.split()
    if len(words) >= 2:
        for i in range(len(words) - 1):
            add(" ".join(words[i:]))
            add(" ".join(words[:i+2]))

    return result[:count]


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
        "до","без","уже","мм","см","кг","шт","г","л","мл","пк",
    }
    return [
        w.strip(".,;:!?\"'()[]") for w in text.lower().split()
        if len(w.strip(".,;:!?\"'()[]")) > 3
        and w.strip(".,;:!?\"'()[]") not in stop
    ]
