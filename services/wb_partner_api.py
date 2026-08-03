"""
Сервис для работы с официальным WB Partner API (suppliers-api.wildberries.ru).

Требует токен продавца (WB_API_TOKEN в .env).
Используется как дополнение к публичному API для получения:
- Частотности поисковых запросов (Analytics API)
- Точных категорий (предметов) для карточки
- Загрузки готовой карточки на WB

Документация: https://openapi.wildberries.ru/
"""

import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://suppliers-api.wildberries.ru"
_CONTENT_URL = "https://content-api.wildberries.ru"
_TIMEOUT = httpx.Timeout(30.0)


def _auth_headers() -> dict[str, str]:
    """Формирует заголовки авторизации для WB Partner API."""
    return {
        "Authorization": settings.WB_API_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def is_wb_api_available() -> bool:
    """Проверяет, настроен ли токен WB Partner API."""
    return bool(settings.WB_API_TOKEN and settings.WB_API_TOKEN.strip())


# ─────────────────────────────────────────────────────────────────────────────
# Поисковая аналитика
# ─────────────────────────────────────────────────────────────────────────────

async def get_search_query_stats(keywords: list[str]) -> dict[str, int]:
    """
    Возвращает частотность поисковых запросов из WB Analytics API.

    Args:
        keywords: список ключевых слов для проверки частотности

    Returns:
        Словарь {keyword: frequency} — от большего к меньшему.
        Пустой словарь если API недоступен или токен не задан.
    """
    if not is_wb_api_available():
        return {}

    result: dict[str, int] = {}

    # WB Analytics: поисковые запросы по периоду
    # Endpoint: GET /api/v1/analytics/keyword-search-stat
    # Берём последние 30 дней
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for kw in keywords[:30]:  # лимит на кол-во запросов
            try:
                resp = await client.get(
                    f"{_BASE_URL}/api/v1/analytics/keyword-search-stat",
                    headers=_auth_headers(),
                    params={"keyword": kw, "period": 30},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # Суммируем показы за период
                    freq = sum(
                        d.get("openCardCount", 0)
                        for d in data.get("data", [])
                    )
                    if freq > 0:
                        result[kw] = freq
                elif resp.status_code == 401:
                    logger.error("WB API: неверный токен авторизации")
                    break
                elif resp.status_code == 403:
                    logger.warning("WB API: нет доступа к Analytics (нужна подписка)")
                    break
            except Exception as e:
                logger.warning("WB API keyword stats error [%s]: %s", kw, e)

    return dict(sorted(result.items(), key=lambda x: x[1], reverse=True))


# ─────────────────────────────────────────────────────────────────────────────
# Категории (предметы)
# ─────────────────────────────────────────────────────────────────────────────

async def get_subject_id(query: str) -> tuple[int | None, str]:
    """
    Находит ID предмета (категории) WB по поисковому запросу.

    Returns:
        (subject_id, subject_name) или (None, "") если не найдено
    """
    if not is_wb_api_available():
        return None, ""

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.get(
                f"{_CONTENT_URL}/content/v2/object/all",
                headers=_auth_headers(),
                params={"name": query, "top": 5, "locale": "ru"},
            )
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("data", [])
                if items:
                    best = items[0]
                    return best.get("subjectID"), best.get("subjectName", "")
            elif resp.status_code == 401:
                logger.error("WB API: неверный токен")
        except Exception as e:
            logger.warning("WB API get_subject_id error: %s", e)

    return None, ""


# ─────────────────────────────────────────────────────────────────────────────
# Загрузка карточки на WB
# ─────────────────────────────────────────────────────────────────────────────

async def upload_card_to_wb(
    nm_id: int,
    title: str,
    description: str,
) -> tuple[bool, str]:
    """
    Обновляет название и описание существующей карточки товара на WB.

    Args:
        nm_id: артикул WB (номенклатура)
        title: новое название (до 60 символов)
        description: новое описание

    Returns:
        (success: bool, message: str)
    """
    if not is_wb_api_available():
        return False, "WB API токен не настроен"

    if len(title) > 60:
        return False, f"Название слишком длинное: {len(title)}/60 символов"

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            # Сначала получаем текущую карточку для patch
            resp_get = await client.post(
                f"{_CONTENT_URL}/content/v2/get/cards/list",
                headers=_auth_headers(),
                json={
                    "settings": {
                        "cursor": {"limit": 1},
                        "filter": {"nmIDs": [nm_id]},
                    }
                },
            )

            if resp_get.status_code == 401:
                return False, "❌ Ошибка авторизации WB API. Проверь токен."

            if resp_get.status_code != 200:
                return False, f"❌ Не удалось получить карточку с WB (status {resp_get.status_code})"

            cards = resp_get.json().get("cards", [])
            if not cards:
                return False, f"❌ Карточка с артикулом {nm_id} не найдена в твоём кабинете WB"

            card = cards[0]

            # Обновляем нужные поля
            card["title"] = title
            card["description"] = description

            resp_update = await client.post(
                f"{_CONTENT_URL}/content/v2/cards/update",
                headers=_auth_headers(),
                json=[card],
            )

            if resp_update.status_code == 200:
                return True, f"✅ Карточка {nm_id} успешно обновлена на WB!"
            else:
                body = resp_update.text[:200]
                return False, f"❌ Ошибка обновления карточки: {resp_update.status_code} — {body}"

        except Exception as e:
            logger.error("WB API upload_card error: %s", e)
            return False, f"❌ Исключение при загрузке: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Список карточек продавца
# ─────────────────────────────────────────────────────────────────────────────

async def get_seller_cards(limit: int = 10) -> list[dict]:
    """
    Возвращает список карточек товаров продавца из личного кабинета WB.
    Используется для выбора карточки для обновления.
    """
    if not is_wb_api_available():
        return []

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.post(
                f"{_CONTENT_URL}/content/v2/get/cards/list",
                headers=_auth_headers(),
                json={
                    "settings": {
                        "cursor": {"limit": limit},
                        "filter": {"withPhoto": -1},
                    }
                },
            )
            if resp.status_code == 200:
                return resp.json().get("cards", [])
            logger.warning("get_seller_cards status=%d", resp.status_code)
        except Exception as e:
            logger.error("get_seller_cards error: %s", e)

    return []
