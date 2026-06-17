import asyncio
import json
import re
import httpx
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
POLYMARKET_URL = "https://polymarket.com/sports/esports"
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/",
}

KYIV_TZ = ZoneInfo("Europe/Kyiv")

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
MAP_DONE_PRICE = 99.5
SERIES_DONE_PRICE = 99.5
STARTED_TOO_LONG_AGO_HOURS = 12
PRICE_SIDE = os.environ.get("POLYMARKET_PRICE_SIDE", "SELL").upper()

match_states = {}
notified_slugs = set()
reminder_tasks = {}


def now_kyiv():
    return datetime.now(KYIV_TZ)


def format_kyiv_time(utc_str):
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        return dt.astimezone(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return utc_str[:16].replace("T", " ")


def seconds_until_start(start_date_str):
    try:
        dt = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
        return (dt - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return 0


def event_start_time(event):
    return (
        event.get("startTime")
        or event.get("gameStartTime")
        or event.get("scheduledStartTime")
        or event.get("eventStartTime")
        or event.get("startDate")
        or ""
    )


def parse_iso_datetime(utc_str):
    if not utc_str:
        return None
    try:
        return datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    except Exception:
        return None


def has_started(start_date_str):
    dt = parse_iso_datetime(start_date_str)
    return not dt or dt <= datetime.now(timezone.utc)


def is_relevant_match(event):
    """
    Перевіряє чи матч актуальний:
    - не закритий і не заархівований
    - startDate не більше 24 годин тому (по Києву)
    """
    if event.get("closed") or event.get("archived"):
        return False

    start_str = event_start_time(event)
    if not start_str:
        return True  # Немає дати — пропускаємо перевірку

    try:
        dt = parse_iso_datetime(start_str)
        if not dt:
            return True
        now_utc = datetime.now(timezone.utc)
        # Пропускаємо матчі що почались занадто давно
        hours_ago = (now_utc - dt).total_seconds() / 3600
        if hours_ago > STARTED_TOO_LONG_AGO_HOURS:
            return False
    except Exception:
        pass

    return True


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


async def get_json(client, url, **kwargs):
    r = await client.get(url, **kwargs)
    r.raise_for_status()
    return r.json()


async def get_cs2_slugs_from_page():
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(POLYMARKET_URL, headers=HTTP_HEADERS)
            r.raise_for_status()
            all_slugs = re.findall(r'cs2-[a-z0-9-]+', r.text)
            seen = set()
            result = []
            for slug in all_slugs:
                parts = slug.split("-")
                if len(parts) >= 5:
                    tail = "-".join(parts[-3:])
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
            data = await get_json(c, f"{GAMMA_API}/events", params={"slug": slug, "limit": 1}, headers=HTTP_HEADERS)
            if data and isinstance(data, list):
                return data[0]
    except Exception as e:
        print(f"Помилка get_event({slug}): {e}")
    return None


def parse_json_list(value):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except Exception:
        return []


def cents(price):
    value = float(price) * 100
    if value < 10 or value > 90:
        return round(value, 1)
    return int(value + 0.5)


async def apply_live_clob_prices(data):
    markets = [data.get("series"), data.get("map1"), data.get("map2"), data.get("map3")]
    token_to_market = {}
    requests = []

    for market in markets:
        if not market:
            continue
        for index, token_id in enumerate(market.get("token_ids", [])[:2]):
            if not token_id:
                continue
            token_to_market[token_id] = (market, index)
            requests.append({"token_id": token_id, "side": PRICE_SIDE})

    if not requests:
        return data

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{CLOB_API}/prices", json=requests, headers=HTTP_HEADERS)
            r.raise_for_status()
            prices = r.json()

        for token_id, side_prices in prices.items():
            price = side_prices.get(PRICE_SIDE)
            if price is None or token_id not in token_to_market:
                continue
            market, index = token_to_market[token_id]
            if index == 0:
                market["price1"] = cents(price)
            elif index == 1:
                market["price2"] = cents(price)
            market["price_source"] = f"clob:{PRICE_SIDE}"
    except Exception as e:
        print(f"Помилка CLOB prices: {e}")

    return data


async def get_parsed_event(slug):
    event = await get_event_by_slug(slug)
    if not event:
        return None, None
    data = parse_markets(event)
    await apply_live_clob_prices(data)
    return event, data


def is_market_resolved(market):
    return bool(
        market.get("closed")
        or market.get("resolved")
        or market.get("archived")
        or market.get("resolutionStatus")
        or market.get("resolvedBy")
    )


def resolved_winner(outcomes, prices, market, threshold):
    if len(outcomes) < 2 or len(prices) < 2:
        return None

    winner_from_api = (
        market.get("winner")
        or market.get("winningOutcome")
        or market.get("resolvedOutcome")
        or market.get("outcome")
    )
    if winner_from_api in outcomes:
        return winner_from_api

    if not is_market_resolved(market) and max(prices) * 100 < threshold:
        return None

    if prices[0] > prices[1] and prices[0] * 100 >= threshold:
        return outcomes[0]
    if prices[1] > prices[0] and prices[1] * 100 >= threshold:
        return outcomes[1]
    return None


def is_live_market(market_data):
    if not market_data or market_data.get("winner"):
        return False
    return 5 < market_data["price1"] < 95 and 5 < market_data["price2"] < 95


def parse_markets(event):
    markets = event.get("markets", [])
    result = {
        "title": event.get("title", ""),
        "start_date": event_start_time(event),
        "series": None, "map1": None, "map2": None, "map3": None,
    }
    for m in markets:
        question = m.get("question", "").lower()
        market_type = (m.get("sportsMarketType") or "").lower()
        group_title = (m.get("groupItemTitle") or "").lower()
        volume = float(m.get("volumeNum", 0) or 0)
        outcomes = m.get("outcomes", "[]")
        prices_str = m.get("outcomePrices", "[]")
        token_ids = parse_json_list(m.get("clobTokenIds", "[]"))
        try:
            ol = parse_json_list(outcomes)
            pl = parse_json_list(prices_str)
            pf = [float(p) for p in pl]
        except Exception:
            continue
        if len(ol) < 2 or len(pf) < 2:
            continue
        is_series_market = (
            market_type == "moneyline"
            or group_title == "match winner"
            or (("series" in question or "moneyline" in question) and "map" not in question)
        )
        done_threshold = SERIES_DONE_PRICE if is_series_market else MAP_DONE_PRICE
        md = {
            "team1": ol[0], "team2": ol[1],
            "price1": cents(pf[0]), "price2": cents(pf[1]),
            "volume": volume,
            "winner": resolved_winner(ol, pf, m, done_threshold),
            "resolved": is_market_resolved(m),
            "token_ids": token_ids,
            "price_source": "gamma",
        }
        if is_series_market:
            result["series"] = md
        elif group_title == "map 1 winner" or "map 1 winner" in question or "map1 winner" in question:
            result["map1"] = md
        elif group_title == "map 2 winner" or "map 2 winner" in question or "map2 winner" in question:
            result["map2"] = md
        elif group_title == "map 3 winner" or "map 3 winner" in question or "map3 winner" in question:
            result["map3"] = md
    return result


def get_match_stage(data):
    series = data.get("series")
    map1 = data.get("map1")
    map2 = data.get("map2")
    map3 = data.get("map3")
    if not series:
        return "unknown"

    if map3 and map3.get("winner"):
        return "finished"

    map1_winner = map1.get("winner") if map1 else None
    map2_winner = map2.get("winner") if map2 else None

    if map2_winner:
        if map1_winner and map1_winner == map2_winner:
            return "map2_done_sweep"
        if map1_winner and map1_winner != map2_winner:
            return "map2_done_split"
        return "map2_done_sweep" if max(series["price1"], series["price2"]) >= 90 else "map2_done_split"

    if series.get("winner"):
        return "finished"

    if map1_winner:
        if is_live_market(map2):
            return "map2_live"
        return "map1_done"

    if not has_started(data.get("start_date", "")):
        return "before"

    if is_live_market(map1):
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
💧 Об'єм серії: ${series['volume']:,.0f}
💧 Об'єм К1: ${map1_liq:,.0f}

{verdict_icon} <b>Вердикт:</b> {verdict}"""

    await send_telegram(msg)
    print(f"[1] {title} | {start} | {verdict}")


# ============================================================
# ПОВІДОМЛЕННЯ 2 — Нагадування за 30 хвилин
# ============================================================
async def msg2_reminder(slug):
    event, data = await get_parsed_event(slug)
    if not event:
        return
    series = data.get("series")
    map1 = data.get("map1")
    title = data.get("title", slug)
    if not series:
        return

    fav_price = max(series["price1"], series["price2"])
    fav_name = series["team1"] if series["price1"] >= series["price2"] else series["team2"]
    dog_name = series["team2"] if series["price1"] >= series["price2"] else series["team1"]
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

    msg += f"\n\n{'✅' if liq_ok else '⚠️'} <b>Вердикт:</b> {'Кандидат — слідкуй за К1!' if liq_ok else 'Пропустити (мало ліквідності К1)'}"

    await send_telegram(msg)
    print(f"[2] {title}")


async def schedule_reminder(slug, start_date_str):
    if not start_date_str:
        return
    secs = seconds_until_start(start_date_str)
    delay = secs - 30 * 60
    if delay > 0:
        await asyncio.sleep(delay)
    state = match_states.get(slug, {})
    if state.get("stage") in ["before", "map1_live"] and not state.get("notified_reminder"):
        state["notified_reminder"] = True
        await msg2_reminder(slug)


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

    winner_k1 = map1.get("winner") if map1 else None
    match_states[slug]["map1_winner"] = winner_k1

    cur_fav, cur_fav_p = (series["team1"], series["price1"]) if series["price1"] >= series["price2"] else (series["team2"], series["price2"])
    cur_dog, cur_dog_p = (series["team2"], series["price2"]) if series["price1"] >= series["price2"] else (series["team1"], series["price1"])

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
  При 1:1 серія {cur_fav} → ~{revert}¢

📋 <b>ЩО ВПИСАТИ В КАЛЬКУЛЯТОР:</b>"""

        if map2 and 5 < map2["price1"] < 95:
            s1 = series["price1"] if series["team1"] == map2["team1"] else series["price2"]
            s2 = series["price2"] if series["team2"] == map2["team2"] else series["price1"]
            msg += f"""
  Команда A: <b>{map2['team1']}</b>
  A · Матч (К2): <b>{map2['price1']}</b>
  A · Серія: <b>{s1}</b>
  Команда B: <b>{map2['team2']}</b>
  B · Матч (К2): <b>{map2['price2']}</b>
  B · Серія: <b>{s2}</b>

  Якщо <b>{cur_fav}</b> бере К2 → sweep 2:0:
    {cur_fav} серія → <b>100</b> / {cur_dog} → <b>0</b>
  Якщо <b>{cur_dog}</b> бере К2 → split 1:1:
    {cur_fav} серія → <b>{revert}</b> / {cur_dog} → <b>{100-revert}</b>"""
    elif not show_forecast and band_range:
        msg += f"\n\n⚠️ Прогноз не застосовний: фаворит {cur_fav_p}¢ — поза робочим діапазоном"

    msg += "\n\n⚡️ <b>У тебе ~5 хвилин — відкривай калькулятор!</b>"
    await send_telegram(msg)
    print(f"[3] К1 done: {title}")


# ============================================================
# ПОВІДОМЛЕННЯ 4a — Sweep 2:0
# ============================================================
async def msg4a_sweep(slug, data):
    series = data.get("series")
    title = data.get("title", slug)
    if not series:
        return
    winner = series.get("winner") or (series["team1"] if series["price1"] > series["price2"] else series["team2"])
    await send_telegram(
        f"🏆 <b>СЕРІЯ ЗАКІНЧИЛАСЬ! 2:0 SWEEP</b>\n"
        f"⚔️ <b>{title}</b>\n"
        f"🥇 Переможець: <b>{winner}</b>\n\n"
        f"✅ Обидві ноги резолвляться автоматично\n"
        f"💰 Профіт зарахується на рахунок Polymarket"
    )
    print(f"[4a] Sweep: {title}")


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
        map2_winner = map2.get("winner")

    msg = f"""⚖️ <b>К2 ЗАКІНЧИЛАСЬ! РАХУНОК 1:1</b>
⚔️ <b>{title}</b>
🗺 К2 взяв: <b>{map2_winner or "невідомо"}</b>

📈 <b>Серія зараз:</b>
  {series['team1']} {series['price1']}¢ / {series['team2']} {series['price2']}¢"""

    if map3 and map3["volume"] > 0:
        msg += f"""
🗺 <b>К3:</b>
  {map3['team1']} {map3['price1']}¢ / {map3['team2']} {map3['price2']}¢"""

    msg += """

✅ <b>Що робити:</b>
Нога «К2» резолвиться в 100¢ → профіт зафіксовано
<b>Продай ногу «СЕРІЯ»</b> по поточній ціні прямо зараз!
Не чекай К3 — фіксуй гарантований профіт

⚡️ Відкривай Polymarket і продавай ногу серії!"""

    await send_telegram(msg)
    print(f"[4b] Split 1:1: {title}")


# ============================================================
# ПОВІДОМЛЕННЯ 5 — Фінал після К3
# ============================================================
async def msg5_final(slug, data):
    series = data.get("series")
    title = data.get("title", slug)
    if not series:
        return
    winner = series.get("winner") or (series["team1"] if series["price1"] > series["price2"] else series["team2"])
    await send_telegram(
        f"🏁 <b>СЕРІЯ ЗАВЕРШЕНА (К3)</b>\n"
        f"⚔️ <b>{title}</b>\n"
        f"🥇 Переможець: <b>{winner}</b>\n"
        f"📊 Рахунок карт: 2:1\n\n"
        f"✅ Серія нога резолвиться автоматично\n"
        f"💰 Профіт зарахується на рахунок"
    )
    print(f"[5] Фінал К3: {title}")


# ============================================================
# СКАНУВАННЯ
# ============================================================
async def scan_matches():
    while True:
        try:
            slugs = await get_cs2_slugs_from_page()
            print(f"[SCAN] {now_kyiv().strftime('%H:%M')} Київ | Slugs: {len(slugs)}")

            for slug in slugs:
                if slug in notified_slugs:
                    continue

                event, data = await get_parsed_event(slug)
                if not event:
                    continue

                # Фільтр: тільки актуальні матчі
                if not is_relevant_match(event):
                    notified_slugs.add(slug)
                    print(f"[SKIP] Старий або закритий: {slug}")
                    continue

                series = data.get("series")
                if not series:
                    continue

                stage = get_match_stage(data)

                # Пропускаємо вже завершені
                if stage == "finished":
                    notified_slugs.add(slug)
                    print(f"[SKIP] Завершений: {slug}")
                    continue

                match_states[slug] = {
                    "title": data["title"],
                    "start_date": data.get("start_date", ""),
                    "stage": stage,
                    "map1_winner": None,
                    "notified_map1": False,
                    "notified_reminder": False,
                    "notified_map2": False,
                    "notified_final": False,
                    "was_sweep": False,
                }
                notified_slugs.add(slug)

                if stage == "before":
                    task = asyncio.create_task(
                        schedule_reminder(slug, data.get("start_date", ""))
                    )
                    reminder_tasks[slug] = task
                elif stage == "map1_live" and not match_states[slug]["notified_reminder"]:
                    match_states[slug]["notified_reminder"] = True
                    await msg2_reminder(slug)

                if series["volume"] >= MIN_SERIES_LIQUIDITY:
                    await msg1_match_found(slug, data)
                else:
                    print(f"[REMINDER ONLY] Мало ліквідності ${series['volume']:,.0f}: {data['title']}")

        except Exception as e:
            print(f"Помилка scan: {e}")

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

                event, data = await get_parsed_event(slug)
                if not event:
                    continue

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

                # Sweep 2:0
                if stage == "map2_done_sweep" and not state["notified_map2"]:
                    state["notified_map2"] = True
                    state["notified_final"] = True
                    state["was_sweep"] = True
                    await msg4a_sweep(slug, data)

                # Split 1:1
                if stage == "map2_done_split" and not state["notified_map2"]:
                    state["notified_map2"] = True
                    await msg4b_split(slug, data)

                # Фінал після К3
                if stage == "finished" and not state["notified_final"]:
                    state["notified_final"] = True
                    if not state.get("was_sweep"):
                        await msg5_final(slug, data)

        except Exception as e:
            print(f"Помилка check: {e}")

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
        "🔍 1 — матч знайдено\n"
        "⏰ 2 — нагадування за 30 хв\n"
        "🎮 3 — після К1 + калькулятор\n"
        "🏆 4 — після К2 (sweep або 1:1)\n"
        "🏁 5 — фінал після К3"
    )
    await asyncio.gather(
        scan_matches(),
        check_active_matches(),
    )


if __name__ == "__main__":
    asyncio.run(main())
