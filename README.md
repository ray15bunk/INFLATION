# cbs-index — индексы цен ЦСБ Израиля

Получение актуальных значений индексов цен из официального REST API Центрального
статистического бюро Израиля (`api.cbs.gov.il`). HTML не парсится — API отдаёт
готовый JSON, включая изменение за месяц и за 12 месяцев.

## Установка

```bash
pip install -r requirements.txt   # httpx
```

## Использование

```bash
# ИПЦ + индекс ресурсов жилищного строительства, последние значения
python cbs_index.py

# на английском, в файл
python cbs_index.py --lang en --out data/latest.json

# последние 13 месяцев по ИПЦ
python cbs_index.py --codes 120010 --history 13

# в автоматизации: убедиться, что данные уже за прошлый месяц (иначе exit 2)
python cbs_index.py --expect-period prev-month --out data/latest.json
```

Как библиотека:

```python
from cbs_index import CPI, RESIDENTIAL_BUILDING_INPUT, fetch_index, fetch_indices

cpi = fetch_index(CPI)
print(cpi.value, cpi.change_1m_pct, cpi.change_12m_pct, cpi.period)

payload = fetch_indices([CPI, RESIDENTIAL_BUILDING_INPUT], strict=False)
```

## Коды индексов

| Код | Индекс |
|-----|--------|
| `120010` | מדד המחירים לצרכן — כללי (ИПЦ, общий) |
| `200010` | מדד מחירי תשומה בבנייה למגורים (ресурсы жилищного строительства) |
| `800010` | ресурсы коммерческого и офисного строительства |
| `240010` | ресурсы дорожного и мостового строительства |
| `40010`  | индекс цен на жильё (раз в два месяца, 3 последних значения предварительные) |

Полный список: <https://api.cbs.gov.il/index/catalog/catalog?format=json>

## Коды выхода CLI

| Код | Значение |
|-----|----------|
| `0` | успех |
| `1` | ошибка сети / структуры ответа / часть индексов не получена |
| `2` | данные ещё не обновились до периода из `--expect-period` |

## Расписание публикаций

ЦСБ публикует индексы **15-го числа в 18:30** по израильскому времени.
Если 15-е приходится на пятницу или канун праздника — в 14:00.

GitHub Actions работает в UTC, а Израиль переходит на летнее время, поэтому
workflow запускается в `45 16 15 * *` UTC: это 19:45 IDT летом и 18:45 IST зимой —
всегда после публикации. См. [.github/workflows/cbs-index.yml](.github/workflows/cbs-index.yml).
