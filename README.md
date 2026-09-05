# livesoccertv-api

Ежедневно обновляемые JSON-фиды с футбольным расписанием, стадионами и
ТВ-трансляциями по странам. Данные собираются из открытого API Fotmob
и коммитятся обратно в этот репозиторий через GitHub Actions.

## Фиды

| Файл | Что внутри | Обновление |
|---|---|---|
| `date/<год>/<ГГГГММДД>.json` | матчи дня: стадион + ТВ-каналы по странам | ежедневно, 32 дня вперёд |
| `football-data.json` | футбольные фикстуры Fotmob | ежедневно |
| `today.json` | матчи на сегодня | ежедневно |
| `cricket-data.json` | крикет, NDTV | еженедельно, вс |

Прямой доступ к любому файлу:

```
https://raw.githubusercontent.com/<owner>/livesoccertv-api/main/date/2026/20260905.json
```

## Формат записи матча

```json
{
    "match_id": 5795443,
    "kickoff": 1788564600,
    "fixture": "Newcastle vs Bournemouth",
    "league_id": 47,
    "league": "Premier League",
    "venue": "St. James' Park",
    "tv_channels": [
        {
            "country": "Austria",
            "country_code": "AT",
            "channels": ["Sky Sports Premier League", "Sky Stream", "Sky X"]
        }
    ]
}
```

`kickoff` — unix-время в секундах, UTC.

## Как это работает

`future_scraper.py` запускается по расписанию на раннере GitHub, ходит в
`https://www.fotmob.com/api/data`, перезаписывает файлы в `date/` и коммитит
их обратно. Ключей и секретов не нужно: Actions выдаёт заданию временный
`GITHUB_TOKEN`, права даёт строка `permissions: contents: write`.

Сбор ТВ-данных опирается на массовый эндпоинт Fotmob `tvlistings`, который
отдаёт все трансляции страны одним ответом, сгруппированные по `match_id`.
Поэтому весь ТВ-срез по 249 странам стоит 249 запросов и занимает ~3 секунды,
а не запрос на каждую пару «матч + страна».

Полный прогон на 32 дня — около 12 минут; основное время уходит на
подтягивание стадионов (`matchDetails`, по запросу на матч).

## Важное ограничение

**У Fotmob горизонт ТВ-данных примерно 10 дней.** В файлах на более далёкие
даты `tv_channels` будет пустым — вещатели ещё не объявлены. Это не сбой:
поле заполнится само, когда дата приблизится и скрипт перезапишет файл.

## Запуск вручную

Через вкладку Actions → нужный workflow → **Run workflow** (доступно благодаря
`workflow_dispatch:`). Локально:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python future_scraper.py
```

## Расписание

Задачи разведены по времени, чтобы не толкаться при push в одну ветку:

| Workflow | UTC |
|---|---|
| `lstv.yml` — `future_scraper.py` | 00:00 ежедневно |
| `fotmob-football.yml` | 00:30 ежедневно |
| `football_com.yml` | 00:45 ежедневно |
| `ndtv-cricket.yml` | 01:15 по воскресеньям |

Фактический запуск обычно на 30–200 минут позже: расписания Actions не
гарантируют точную минуту, задача ждёт в общей очереди GitHub.
