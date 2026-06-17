import asyncio
import json
import re
import httpx
import os
from datetime import datetime, timezone, date, timedelta

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_URL = "https://polymarket.com/sports/esports"
KYIV_TZ_OFFSET = 3  # UTC+3

BAND_SHIFTS = {
    (50, 55): {"fav_win": 22, "dog_win": -24, "revert": 52},
    (55, 60): {"fav_win": 20, "dog_win": -25, "revert": 55},
    (60, 65): {"fav_win": 18.5, "dog_win": -25, "revert": 56},
    (65, 70): {"fav_win": 17, "dog_win": -26, "revert": 58},
    (70, 75): {"fav_win": 13.6, "dog_win": -26, "revert": 60},
    (75, 80): {"fav_win": 11.5, "dog_win": -24, "revert": 62},
    (80, 85): {"fav_win": 9.5, "dog_win": -25, "revert": 65},
    (85, 95): {"fav_win": 6, "dog_win": -24, "revert": 68},
}

MIN_SERIES_LIQUIDITY = 30_000
MIN_MAP_LIQUIDITY = 3_000

match_states = {}
notified_slugs = set()
reminder_tasks = {}

KYIV_TZ = timezone(timedelta(hours=KYIV_TZ_OFFSET))


def now_kyiv():
    return datetime.now(KYIV_TZ)


def format_kyiv_time(utc_str):
    """Конвертує UTC рядок в час Києва"""
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        dt_kyiv = dt.astimezone(KYIV_TZ)
        return dt_kyiv.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return utc_str[:16].replace("T", " ")


def seconds_until_start(start_date_str):
    try:
        dt = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (dt - now).total_seconds()
    except Exception:
        return 0


def get_band(fav_price):
    for (lo, hi), data in BAND_SHIFTS.items():
        if lo <= fav_price < hi:
            return (lo, hi), data
    return None, None


async def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML"
            })
    except Exception as e:
        print(f"Помилка відправки: {e}")


async def get_cs2_slugs_from_page():
    """Парсить сторінку і повертає всі CS2 slugs без обмежень по даті"""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(POLYMARKET_URL, headers={"User-Agent": "Mozilla/5.0"})
            all_slugs = re.findall(r'cs2-[a-z0-9-]+', r.text)
            seen = set()
            result = []
            for slug in all_slugs:
                parts = slug.split("-")
                if len(parts) >= 5:
                    tail = "-".join(parts[-3:])
                    # Основний slug закінчується датою
                    if re.match(r'^\d{4}-\d{2}-\d{2}$', tail) and slug not in seen:
                        seen.add(slug)
                        result.append(slug)
            return result
    except Exception as e:
        print(f"Помилка парсингу: {e}")
        return []


async def get_event_by_slug(slug):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{GAMMA_API}/events", params={"slug": slug, "limit": 1})
            data = r.json()
            if data and isinstance(data, list):
                return data[0]
    except Exception as e:
        print(f"Помилка get_event({slug}): {e}")
    return None


def parse_markets(event):
    markets = event.get("markets", [])
    result = {
        "title": event.get("title", ""),
        "start_date": event.get("startDate", ""),
        "series": None,
        "map1": None,
        "map2": None,
        "map3": None,
    }
    for m in markets:
        question = m.get("question", "").lower()
        volume = float(m.get("volumeNum", 0) or 0)
        outcomes = m.get("outcomes", "[]")
        prices_str = m.get("outcomePrices", "[]")
        try:
            outcomes_list = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
            prices_list = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
            prices_float = [float(p) for p in prices_list]
        except Exception:
            continue
        if len(outcomes_list) < 2 or len(prices_float) < 2:
            continue
        md = {
            "team1": outcomes_list[0],
            "team2": outcomes_list[1],
            "price1": round(prices_float[0] * 100),
            "price2": round(prices_float[1] * 100),
            "volume": volume,
            "winner": outcomes_list[0] if prices_float[0] >= 0.95 else (
                outcomes_list[1] if prices_float[1] >= 0.95 else None
            ),
        }
        if ("series" in question or "moneyline" in question or "winner" in question) and "map" not in question:
            result["series"] = md
        elif "map 1" in question or "map1" in question:
            result["map1"] = md
        elif "map 2" in question or "map2" in question:
            result["map2"] = md
        elif "map 3" in question or "map3" in question:
            result["map3"] = md
    return result


def get_match_stage(data):
    series = data.get("series")
    map1 = data.get("map1")
    map2 = data.get("map2")
    map3 = data.get("map3")

    if not series:
        return "unknown"

    s1, s2 = series["price1"], series["price2"]

    if s1 >= 98 or s2 >= 98:
        return "finished"

    if map3:
        if map3["price1"] >= 95 or map3["price2"] >= 95:
            return "finished"
        return "map3_live"

    if map2:
        p1, p2 = map2["price1"], map2["price2"]
        if p1 >= 95 or p2 >= 95:
            if s1 >= 90 or s2 >= 90:
                return "map2_done_sweep"
            return "map2_done_split"
        if 5 < p1 < 95 and 5 < p2 < 95:
            return "map2_live"
        return "map1_done"

    if map1:
        p1, p2 = map1["price1"], map1["price2"]
        if p1 >= 95 or p2 >= 95:
            return "map1_done"
        if 5 < p1 < 95 and 5 < p2 < 95:
            return "map1_live"

    return "before"


# ============================================================
# ПОВІДОМЛЕННЯ 1 — Матч знайдено
# ============================================================
async def msg1_match_found(slug, data):
    series = data.get("series")
    map1 = data.get("map1")
    title = data.get("title", slug)
    start = format_kyiv_time(data.get("start_date", ""))

    fav_price = max(series["price1"], series["price2"])
    band_range, _ = get_band(fav_price)
    map1_liq = map1["volume"] if map1 else 0
    liq_ok = series["volume"] >= MIN_SERIES_LIQUIDITY and map1_liq >= MIN_MAP_LIQUIDITY and band_range and band_range[0] <= 70
    verdict_icon = "✅" if liq_ok else "⚠️"
    verdict = "Кандидат для торгівлі" if liq_ok else "Пропустити"

    msg = f"""🔍 <b>ЗНАЙДЕНО CS2 МАТЧ</b>
⚔️ <b>{title}</b>
🕐 Початок (Київ): {start}

📊 <b>Серія:</b> {series['team1']} {series['price1']}¢ / {series['team2']} {series['price2']}¢
💧 Об'єм серії: ${series['volume']:,.0f}"""

    if map1:
        msg += f"\n💧 Об'єм К1: ${map1_liq:,.0f}"

    msg += f"\n\n{verdict_icon} <b>Вердикт:</b> {verdict}"
    await send_telegram(msg)
    print(f"[1 ЗНАЙДЕНО] {title} | Старт: {start}")


# ============================================================
# ПОВІДОМЛЕННЯ 2 — Нагадування за 30 хвилин
# ============================================================
async def msg2_reminder_30min(slug):
    event = await get_event_by_slug(slug)
    if not event:
        return
    data = parse_markets(event)
    series = data.get("series")
    map1 = data.get("map1")
    title = data.get("title", slug)

    if not series or series["volume"] < MIN_SERIES_LIQUIDITY:
        return

    fav_price = max(series["price1"], series["price2"])
    if series["price1"] >= series["price2"]:
        fav_name, dog_name = series["team1"], series["team2"]
    else:
        fav_name, dog_name = series["team2"], series["team1"]

    band_range, band_data = get_band(fav_price)
    map1_liq = map1["volume"] if map1 else 0
    liq_ok = map1_liq >= MIN_MAP_LIQUIDITY and band_range and band_range[0] <= 70

    msg = f"""⏰ <b>МАТЧ ЧЕРЕЗ 30 ХВИЛИН!</b>
⚔️ <b>{title}</b>

📊 <b>Серія:</b> {series['team1']} {series['price1']}¢ / {series['team2']} {series['price2']}¢
💧 Об'єм серії: ${series['volume']:,.0f}"""

    if map1:
        msg += f"""
🗺 <b>К1:</b> {map1['team1']} {map1['price1']}¢ / {map1['team2']} {map1['price2']}¢
💧 Об'єм К1: ${map1_liq:,.0f}"""

    if band_range and band_data:
        msg += f"""

📉 <b>Діапазон фаворита ({fav_name}):</b> {band_range[0]}-{band_range[1]}%
   Якщо {fav_name} бере К1: серія +{band_data['fav_win']}
   Якщо {dog_name} бере К1: серія {band_data['dog_win']}"""

    if liq_ok:
        msg += f"\n\n✅ <b>Вердикт:</b> Кандидат для торгівлі\n👁 Слідкуй за К1!"
    else:
        msg += f"\n\n⚠️ <b>Вердикт:</b> Пропустити (мало ліквідності К1)"

    await send_telegram(msg)
    print(f"[2 30ХВ] {title}")


async def schedule_reminder(slug, start_date_str):
    secs = seconds_until_start(start_date_str)
    delay = secs - 30 * 60
    if delay > 0:
        print(f"[ПЛАН] Нагадування через {delay:.0f}с для {slug}")
        await asyncio.sleep(delay)
    stage = match_states.get(slug, {}).get("stage", "before")
    if stage in ["before", "map1_live"]:
        await msg2_reminder_30min(slug)


# ============================================================
# ПОВІДОМЛЕННЯ 3 — Після К1
# ============================================================
async def msg3_map1_done(slug, data):
    series = data.get("series")
    map1 = data.get("map1")
    map2 = data.get("map2")
    title = data.get("title", slug)

    if not series or series["volume"] < MIN_SERIES_LIQUIDITY:
        return

    winner_k1 = None
    if map1:
        winner_k1 = map1.get("winner") or (
            map1["team1"] if map1["price1"] >= 95 else map1["team2"]
        )
    match_states[slug]["map1_winner"] = winner_k1

    if series["price1"] >= series["price2"]:
        cur_fav, cur_fav_p = series["team1"], series["price1"]
        cur_dog, cur_dog_p = series["team2"], series["price2"]
    else:
        cur_fav, cur_fav_p = series["team2"], series["price2"]
        cur_dog, cur_dog_p = series["team1"], series["price1"]

    band_range, band_data = get_band(cur_fav_p)
    show_forecast = band_range and 50 <= band_range[0] and band_range[1] <= 80

    msg = f"""🎮 <b>К1 ЗАКІНЧИЛАСЬ!</b>
⚔️ <b>{title}</b>
🏆 К1 взяв: <b>{winner_k1 or "невідомо"}</b>

📈 <b>Серія зараз:</b>
  {cur_fav} {cur_fav_p}¢ / {cur_dog} {cur_dog_p}¢
  Об'єм: ${series['volume']:,.0f}"""

    if map2 and 5 < map2["price1"] < 95:
        msg += f"""

🗺 <b>К2:</b>
  {map2['team1']} {map2['price1']}¢ / {map2['team2']} {map2['price2']}¢
  Об'єм: ${map2['volume']:,.0f}"""

    if show_forecast and band_data:
        revert = band_data["revert"]
        msg += f"""

🤖 <b>Прогноз (база {band_range[0]}-{band_range[1]}%):</b>
  При 1:1 серія {cur_fav} → ~{revert}¢"""

        if map2 and 5 < map2["price1"] < 95:
            s_price1 = series["price1"] if series["team1"] == map2["team1"] else series["price2"]
            s_price2 = series["price2"] if series["team2"] == map2["team2"] else series["price1"]

            msg += f"""

📋 <b>ЩО ВПИСАТИ В КАЛЬКУЛЯТОР:</b>
  Команда A: <b>{map2['team1']}</b>
  A · Матч (К2 ціна): <b>{map2['price1']}</b>
  A · Серія: <b>{s_price1}</b>
  Команда B: <b>{map2['team2']}</b>
  B · Матч (К2 ціна): <b>{map2['price2']}</b>
  B · Серія: <b>{s_price2}</b>

  Якщо <b>{cur_fav}</b> бере К2 (sweep 2:0):
    {cur_fav} серія → <b>100</b>
    {cur_dog} серія → <b>0</b>

  Якщо <b>{cur_dog}</b> бере К2 (split 1:1):
    {cur_fav} серія → <b>{revert}</b>
    {cur_dog} серія → <b>{100 - revert}</b>"""

    elif not show_forecast and band_range:
        msg += f"\n\n⚠️ Прогноз не застосовний: фаворит {cur_fav_p}¢ — поза робочим діапазоном стратегії"

    msg += "\n\n⚡️ <b>У тебе ~5 хвилин — відкривай калькулятор!</b>"
    await send_telegram(msg)
    print(f"[3 К1] {title}: {winner_k1}")


# ============================================================
# ПОВІДОМЛЕННЯ 4a — Sweep 2:0
# ============================================================
async def msg4a_sweep(slug, data):
    series = data.get("series")
    title = data.get("title", slug)
    if not series:
        return
    winner = series["team1"] if series["price1"] >= 95 else series["team2"]
    await send_telegram(
        f"🏆 <b>СЕРІЯ ЗАКІНЧИЛАСЬ! 2:0 SWEEP</b>\n"
        f"⚔️ <b>{title}</b>\n"
        f"🥇 Переможець: <b>{winner}</b>\n\n"
        f"✅ Обидві ноги резолвляться автоматично\n"
        f"💰 Профіт зарахується на рахунок Polymarket"
    )
    print(f"[4a SWEEP] {title}: {winner}")


# ============================================================
# ПОВІДОМЛЕННЯ 4b — Split 1:1
# ============================================================
async def msg4b_split(slug, data):
    series = data.get("series")
    map2 = data.get("map2")
    map3 = data.get("map3")
    title = data.get("title", slug)
    if not series:
        return

    map2_winner = None
    if map2:
        map2_winner = map2.get("winner") or (
            map2["team1"] if map2["price1"] >= 95 else map2["team2"]
        )

    msg = f"""⚖️ <b>К2 ЗАКІНЧИЛАСЬ! РАХУНОК 1:1</b>
⚔️ <b>{title}</b>
🗺 К2 взяв: <b>{map2_winner or "невідомо"}</b>

📈 <b>Серія зараз:</b>
  {series['team1']} {series['price1']}¢ / {series['team2']} {series['price2']}¢"""

    if map3 and map3["volume"] > 0:
        msg += f"""
🗺 <b>К3:</b>
  {map3['team1']} {map3['price1']}¢ / {map3['team2']} {map3['price2']}¢"""

    msg += f"""

✅ <b>Що робити:</b>
Нога «К2» резолвиться в 100¢ → профіт зафіксовано
<b>Продай ногу «СЕРІЯ»</b> по поточній ціні прямо зараз!
Не чекай К3 — фіксуй гарантований профіт

⚡️ Відкривай Polymarket і продавай ногу серії!"""

    await send_telegram(msg)
    print(f"[4b SPLIT] {title}: 1:1")


# ============================================================
# ПОВІДОМЛЕННЯ 5 — Фінал після К3
# ============================================================
async def msg5_final(slug, data):
    series = data.get("series")
    title = data.get("title", slug)
    if not series:
        return
    winner = series["team1"] if series["price1"] >= 95 else series["team2"]
    await send_telegram(
        f"🏁 <b>СЕРІЯ ЗАВЕРШЕНА (К3)</b>\n"
        f"⚔️ <b>{title}</b>\n"
        f"🥇 Переможець: <b>{winner}</b>\n"
        f"📊 Рахунок карт: 2:1\n\n"
        f"✅ Серія нога резолвиться автоматично\n"
        f"💰 Профіт зарахується на рахунок"
    )
    print(f"[5 К3] {title}: {winner}")


# ============================================================
# СКАНУВАННЯ НОВИХ МАТЧІВ
# ============================================================
async def scan_matches():
    while True:
        try:
            slugs = await get_cs2_slugs_from_page()
            print(f"[SCAN] Знайдено slugs: {len(slugs)}")

            for slug in slugs:
                if slug in notified_slugs:
                    continue

                event = await get_event_by_slug(slug)
                if not event:
                    continue

                # Пропускаємо закриті/заархівовані матчі
                if event.get("closed") or event.get("archived"):
                    notified_slugs.add(slug)
                    continue

                data = parse_markets(event)
                series = data.get("series")
                if not series:
                    continue

                stage = get_match_stage(data)

                # Пропускаємо вже завершені
                if stage == "finished":
                    notified_slugs.add(slug)
                    continue

                match_states[slug] = {
                    "title": data["title"],
                    "start_date": data.get("start_date", ""),
                    "stage": stage,
                    "map1_winner": None,
                    "notified_map1": False,
                    "notified_map2": False,
                    "notified_final": False,
                    "was_sweep": False,
                }
                notified_slugs.add(slug)

                # Повідомлення 1 — завжди при знаходженні
                await msg1_match_found(slug, data)

                # Плануємо нагадування за 30 хвилин
                if stage == "before":
                    task = asyncio.create_task(
                        schedule_reminder(slug, data.get("start_date", ""))
                    )
                    reminder_tasks[slug] = task
                elif stage == "map1_live":
                    # Матч вже йде — надсилаємо нагадування одразу
                    await msg2_reminder_30min(slug)

        except Exception as e:
            print(f"Помилка scan_matches: {e}")

        await asyncio.sleep(120)


# ============================================================
# ВІДСТЕЖЕННЯ АКТИВНИХ МАТЧІВ
# ============================================================
async def check_active_matches():
    await asyncio.sleep(60)

    while True:
        try:
            for slug, state in list(match_states.items()):
                if state.get("notified_final"):
                    continue

                event = await get_event_by_slug(slug)
                if not event:
                    continue

                data = parse_markets(event)
                series = data.get("series")
                if not series or series["volume"] < MIN_SERIES_LIQUIDITY:
                    continue

                stage = get_match_stage(data)
                prev_stage = state.get("stage", "before")
                state["stage"] = stage

                # К1 закінчилась
                if stage in ["map1_done", "map2_live", "map2_done_sweep",
                             "map2_done_split", "map3_live", "finished"] \
                        and prev_stage in ["before", "map1_live"] \
                        and not state["notified_map1"]:
                    state["notified_map1"] = True
                    await msg3_map1_done(slug, data)

                # К2 закінчилась — sweep 2:0
                elif stage == "map2_done_sweep" and not state["notified_map2"]:
                    state["notified_map2"] = True
                    state["notified_final"] = True
                    state["was_sweep"] = True
                    await msg4a_sweep(slug, data)

                # К2 закінчилась — split 1:1
                elif stage == "map2_done_split" and not state["notified_map2"]:
                    state["notified_map2"] = True
                    await msg4b_split(slug, data)

                # Серія завершена після К3
                elif stage == "finished" and not state["notified_final"]:
                    state["notified_final"] = True
                    if state.get("notified_map2") and not state.get("was_sweep"):
                        await msg5_final(slug, data)

        except Exception as e:
            print(f"Помилка check_active_matches: {e}")

        await asyncio.sleep(90)


# ============================================================
# ЗАПУСК
# ============================================================
async def main():
    kyiv_time = now_kyiv().strftime("%d.%m.%Y %H:%M")
    print(f"Бот запущений. Час Київ: {kyiv_time}")

    await send_telegram(
        f"✅ <b>Polymarket CS2 бот запущений!</b>\n"
        f"🕐 Час Київ: {kyiv_time}\n\n"
        "Буду надсилати:\n"
        "🔍 Повід. 1 — матч знайдено (всі CS2 матчі)\n"
        "⏰ Повід. 2 — нагадування за 30 хв до початку\n"
        "🎮 Повід. 3 — після К1 + що вписати в калькулятор\n"
        "🏆 Повід. 4 — після К2 (sweep або 1:1)\n"
        "🏁 Повід. 5 — фінал серії після К3"
    )

    await asyncio.gather(
        scan_matches(),
        check_active_matches(),
    )


if __name__ == "__main__":
    asyncio.run(main())
