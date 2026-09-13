"""Получение актуальных индексов цен Израиля (ЦСБ / הלשכה המרכזית לסטטיסטיקה).

Источник данных — официальный REST API ЦСБ:
    https://api.cbs.gov.il/index/data/price

Модуль не парсит HTML: API отдаёт готовый JSON со значением индекса в пунктах,
месячным и годовым изменением, поэтому скрейпинг здесь не нужен.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, Final, Mapping, Sequence

import httpx

__all__ = [
    "CBSError",
    "CBSNetworkError",
    "CBSResponseError",
    "CBSNoDataError",
    "IndexReading",
    "fetch_index",
    "fetch_history",
    "fetch_indices",
    "CPI",
    "RESIDENTIAL_BUILDING_INPUT",
    "KNOWN_INDICES",
]

logger = logging.getLogger(__name__)

API_URL: Final[str] = "https://api.cbs.gov.il/index/data/price"

# ЦСБ просит присылать осмысленный User-Agent. Подставьте свой контакт.
USER_AGENT: Final[str] = "cbs-index-fetcher/1.0 (+contact: you@example.com)"

DEFAULT_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(
    connect=10.0, read=30.0, write=10.0, pool=10.0
)
DEFAULT_RETRIES: Final[int] = 3
RETRY_BACKOFF_SECONDS: Final[float] = 2.0

# --- Коды индексов (сверены с https://api.cbs.gov.il/index/catalog/catalog) ---
CPI: Final[int] = 120010
"""מדד המחירים לצרכן - כללי / Индекс потребительских цен, общий."""

RESIDENTIAL_BUILDING_INPUT: Final[int] = 200010
"""מדד מחירי תשומה בבנייה למגורים / Индекс цен ресурсов в жилищном строительстве."""

COMMERCIAL_BUILDING_INPUT: Final[int] = 800010
"""Индекс цен ресурсов в строительстве коммерческих и офисных зданий."""

ROAD_AND_BRIDGE_INPUT: Final[int] = 240010
"""Индекс цен ресурсов в дорожном и мостовом строительстве."""

DWELLING_PRICES: Final[int] = 40010
"""Индекс цен на жильё (раз в два месяца; последние 3 значения — предварительные)."""

KNOWN_INDICES: Final[Mapping[int, str]] = {
    CPI: "Индекс потребительских цен",
    RESIDENTIAL_BUILDING_INPUT: "Индекс цен ресурсов в жилищном строительстве",
    COMMERCIAL_BUILDING_INPUT: "Индекс цен ресурсов в коммерческом строительстве",
    ROAD_AND_BRIDGE_INPUT: "Индекс цен ресурсов в дорожном строительстве",
    DWELLING_PRICES: "Индекс цен на жильё",
}

DEFAULT_INDICES: Final[tuple[int, ...]] = (CPI, RESIDENTIAL_BUILDING_INPUT)


# --------------------------------------------------------------------------- #
# Исключения
# --------------------------------------------------------------------------- #
class CBSError(Exception):
    """Базовая ошибка при работе с API ЦСБ."""


class CBSNetworkError(CBSError):
    """Сеть недоступна, таймаут или сервер вернул ошибку после всех попыток."""


class CBSResponseError(CBSError):
    """Ответ получен, но его структура не соответствует ожидаемой."""


class CBSNoDataError(CBSError):
    """API ответил корректно, но данных по запрошенному индексу нет."""


# --------------------------------------------------------------------------- #
# Модель данных
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IndexReading:
    """Одно значение индекса за конкретный месяц."""

    code: int
    name: str
    name_ru: str | None
    year: int
    month: int
    month_name: str
    period: str
    value: float
    base: str | None
    change_1m_pct: float | None
    change_12m_pct: float | None
    retrieved_at: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        """Представление, пригодное для json.dumps()."""
        return asdict(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# --------------------------------------------------------------------------- #
# Низкоуровневый слой: HTTP + разбор ответа
# --------------------------------------------------------------------------- #
def _request_json(
    params: Mapping[str, Any],
    *,
    client: httpx.Client | None = None,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
) -> dict[str, Any]:
    """GET к API ЦСБ с повторами и экспоненциальной задержкой.

    Raises:
        CBSNetworkError: сеть/таймаут/HTTP-ошибка не устранились за `retries` попыток.
        CBSResponseError: тело ответа — не JSON-объект.
    """
    owns_client = client is None
    http = client or httpx.Client(
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )
    last_error: Exception | None = None
    try:
        for attempt in range(1, retries + 1):
            try:
                response = http.get(API_URL, params=dict(params))
                response.raise_for_status()
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                logger.warning("Попытка %s/%s: сетевая ошибка: %s", attempt, retries, exc)
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code
                logger.warning("Попытка %s/%s: HTTP %s", attempt, retries, status)
                # 4xx (кроме 429) повторять бессмысленно — запрос некорректен.
                if 400 <= status < 500 and status != 429:
                    raise CBSNetworkError(f"API ЦСБ вернул HTTP {status}") from exc
            else:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise CBSResponseError(
                        "Ответ API не является корректным JSON "
                        f"(Content-Type: {response.headers.get('content-type')!r})"
                    ) from exc
                if not isinstance(payload, dict):
                    raise CBSResponseError(
                        f"Ожидался JSON-объект, получен {type(payload).__name__}"
                    )
                return payload

            if attempt < retries:
                time.sleep(RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))

        raise CBSNetworkError(
            f"Не удалось получить данные из API ЦСБ за {retries} попыток: {last_error}"
        ) from last_error
    finally:
        if owns_client:
            http.close()


def _as_float(value: Any) -> float | None:
    """Мягкое приведение к float: API иногда присылает null или строку."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("Не удалось преобразовать %r в число", value)
        return None


def _parse_series(
    payload: Mapping[str, Any], code: int
) -> tuple[str, list[dict[str, Any]]]:
    """Извлекает имя индекса и месячные записи, отсортированные от новых к старым."""
    months = payload.get("month")
    if not months:
        raise CBSNoDataError(
            f"API не вернул месячных данных для индекса {code}. "
            "Проверьте код индекса (https://api.cbs.gov.il/index/catalog/catalog) "
            "или запрошенный период."
        )
    if not isinstance(months, list) or not isinstance(months[0], dict):
        raise CBSResponseError(f"Неожиданная структура поля 'month' для индекса {code}")

    series = months[0]
    name = str(series.get("name") or KNOWN_INDICES.get(code) or f"index {code}").strip()

    records = series.get("date")
    if not isinstance(records, list) or not records:
        raise CBSNoDataError(
            f"Для индекса {code} отсутствуют значения за запрошенный период"
        )

    valid = [
        r for r in records if isinstance(r, dict) and r.get("year") and r.get("month")
    ]
    if not valid:
        raise CBSResponseError(f"Ни одна запись индекса {code} не содержит год и месяц")

    # API отдаёт данные от новых к старым, но полагаться на это не стоит.
    valid.sort(key=lambda r: (int(r["year"]), int(r["month"])), reverse=True)
    return name, valid


def _build_reading(code: int, name: str, record: Mapping[str, Any]) -> IndexReading:
    """Собирает IndexReading из одной записи API."""
    # currBase — значение в текущей базе; prevBase заполняется в месяцы смены базы.
    base_block = record.get("currBase") or record.get("prevBase")
    if not isinstance(base_block, Mapping):
        raise CBSResponseError(
            f"В записи индекса {code} за {record.get('month')}/{record.get('year')} "
            "нет блока currBase/prevBase со значением индекса"
        )

    value = _as_float(base_block.get("value"))
    if value is None:
        raise CBSResponseError(
            f"Пустое значение индекса {code} за {record.get('month')}/{record.get('year')}"
        )

    year = int(record["year"])
    month = int(record["month"])

    return IndexReading(
        code=code,
        name=name,
        name_ru=KNOWN_INDICES.get(code),
        year=year,
        month=month,
        month_name=str(record.get("monthDesc") or calendar.month_name[month]),
        period=f"{year:04d}-{month:02d}",
        value=value,
        base=(str(base_block["baseDesc"]).strip() if base_block.get("baseDesc") else None),
        change_1m_pct=_as_float(record.get("percent")),
        change_12m_pct=_as_float(record.get("percentYear")),
        retrieved_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        source=f"{API_URL}?id={code}",
    )


# --------------------------------------------------------------------------- #
# Публичный API
# --------------------------------------------------------------------------- #
def fetch_index(
    code: int = CPI,
    *,
    lang: str = "he",
    client: httpx.Client | None = None,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
) -> IndexReading:
    """Возвращает последнее опубликованное значение индекса.

    Args:
        code: код индекса ЦСБ (см. KNOWN_INDICES).
        lang: 'he' или 'en' — язык названия индекса и месяца.

    Raises:
        CBSNetworkError, CBSResponseError, CBSNoDataError.
    """
    payload = _request_json(
        {"id": code, "format": "json", "download": "false", "last": 1, "lang": lang},
        client=client,
        timeout=timeout,
        retries=retries,
    )
    name, records = _parse_series(payload, code)
    return _build_reading(code, name, records[0])


def fetch_history(
    code: int = CPI,
    *,
    months: int = 13,
    lang: str = "he",
    client: httpx.Client | None = None,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
) -> list[IndexReading]:
    """Возвращает последние `months` значений индекса, от новых к старым."""
    if months < 1:
        raise ValueError("months должно быть >= 1")
    payload = _request_json(
        {
            "id": code,
            "format": "json",
            "download": "false",
            "last": months,
            "lang": lang,
            "pagesize": max(months, 100),
        },
        client=client,
        timeout=timeout,
        retries=retries,
    )
    name, records = _parse_series(payload, code)
    return [_build_reading(code, name, r) for r in records[:months]]


def fetch_indices(
    codes: Sequence[int] = DEFAULT_INDICES,
    *,
    lang: str = "he",
    strict: bool = True,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
) -> dict[str, Any]:
    """Забирает несколько индексов, переиспользуя одно HTTP-соединение.

    Args:
        strict: True — первая же ошибка пробрасывается наружу;
            False — проблемный индекс попадает в ключ 'errors',
            остальные возвращаются как обычно.
    """
    result: dict[str, Any] = {
        "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "indices": {},
    }
    errors: dict[str, str] = {}

    with httpx.Client(
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    ) as client:
        for code in codes:
            try:
                reading = fetch_index(code, lang=lang, client=client, retries=retries)
            except CBSError as exc:
                if strict:
                    raise
                logger.error("Индекс %s не получен: %s", code, exc)
                errors[str(code)] = f"{type(exc).__name__}: {exc}"
            else:
                result["indices"][str(code)] = reading.to_dict()

    if errors:
        result["errors"] = errors
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _previous_month(today: dt.date) -> str:
    prev = today.replace(day=1) - dt.timedelta(days=1)
    return f"{prev.year:04d}-{prev.month:02d}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Актуальные индексы цен ЦСБ Израиля через официальный REST API."
    )
    parser.add_argument(
        "--codes",
        type=int,
        nargs="+",
        default=list(DEFAULT_INDICES),
        metavar="CODE",
        help=f"коды индексов (по умолчанию: {' '.join(map(str, DEFAULT_INDICES))})",
    )
    parser.add_argument("--lang", choices=("he", "en"), default="he", help="язык названий")
    parser.add_argument(
        "--history", type=int, metavar="N", help="вывести N последних месяцев"
    )
    parser.add_argument("--out", metavar="PATH", help="записать JSON в файл")
    parser.add_argument(
        "--expect-period",
        metavar="YYYY-MM|prev-month",
        help="проверить, что данные относятся к указанному месяцу; иначе код выхода 2",
    )
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        if args.history:
            payload: dict[str, Any] = {
                "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "history": {
                    str(code): [
                        r.to_dict()
                        for r in fetch_history(
                            code,
                            months=args.history,
                            lang=args.lang,
                            retries=args.retries,
                        )
                    ]
                    for code in args.codes
                },
            }
        else:
            payload = fetch_indices(
                args.codes, lang=args.lang, strict=False, retries=args.retries
            )
    except CBSError as exc:
        print(f"Ошибка получения данных ЦСБ: {exc}", file=sys.stderr)
        return 1

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"Записано в {args.out}", file=sys.stderr)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)

    if payload.get("errors"):
        return 1

    if args.expect_period and not args.history:
        expected = (
            _previous_month(dt.date.today())
            if args.expect_period == "prev-month"
            else args.expect_period
        )
        stale = [
            code for code, data in payload["indices"].items() if data["period"] != expected
        ]
        if stale:
            print(
                f"Данные ещё не обновлены до {expected}; "
                f"устаревшие индексы: {', '.join(stale)}",
                file=sys.stderr,
            )
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
