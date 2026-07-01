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
    (50, 55): {"fav_m1_dog_m2_delta": -2.1, "dog_m1_fav_m2_delta": -2.0},
    (55, 60): {"fav_m1_dog_m2_delta": -1.6, "dog_m1_fav_m2_delta": -2.2},
    (60, 65): {"fav_m1_dog_m2_delta": -4.5, "dog_m1_fav_m2_delta": -2.6},
    (65, 70): {"fav_m1_dog_m2_delta": -6.1, "dog_m1_fav_m2_delta": -3.4},
    (70, 75): {"fav_m1_dog_m2_delta": -5.7, "dog_m1_fav_m2_delta": -6.9},
    (75, 80): {"fav_m1_dog_m2_delta": -8.4, "dog_m1_fav_m2_delta": -10.1},
    (80, 85): {"fav_m1_dog_m2_delta": -10.1, "dog_m1_fav_m2_delta": -8.9},
    (85, 96): {"fav_m1_dog_m2_delta": -8.0, "dog_m1_fav_m2_delta": -9.6},
}

MIN_SERIES_LIQUIDITY = 10_000
MIN_MAP_LIQUIDITY = 3_000
MAP_DONE_PRICE = 98.0
SERIES_DONE_PRICE = 99.5
STARTED_TOO_LONG_AGO_HOURS = 12
REMINDER_SECONDS_BEFORE_START = 30 * 60
REMINDER_LATE_GRACE_SECONDS = 10 * 60
SCAN_INTERVAL_SECONDS = 30
ACTIVE_CHECK_INITIAL_DELAY_SECONDS = 15
PRICE_SIDE = os.environ.get("POLYMARKET_PRICE_SIDE", "SELL").upper()

match_states = {}
notified_slugs = set()
reminder_tasks = {}

PREMATCH_SNAPSHOT_SOURCES = {"before", "reminder", "pre_k1", "first_seen"}


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
    top_band = max(BAND_SHIFTS.keys(), key=lambda band: band[1])
    if fav_price >= top_band[1]:
        return top_band, BAND_SHIFTS[top_band]
    return None, None


def capture_prematch_snapshot(slug, data, source="first_seen", force=False):
    series = data.get("series") if data else None
    if not series:
        return

    state = match_states.setdefault(slug, {})
    current_source = state.get("prematch_source")
    if current_source in {"before", "reminder"} and not force:
        return
    if state.get("prematch_fav") and current_source in PREMATCH_SNAPSHOT_SOURCES and not force:
        return

    team1_fav = series["price1"] >= series["price2"]
    state["prematch_fav"] = series["team1"] if team1_fav else series["team2"]
    state["prematch_fav_price"] = max(series["price1"], series["price2"])
    state["prematch_dog"] = series["team2"] if team1_fav else series["team1"]
    state["prematch_dog_price"] = min(series["price1"], series["price2"])
    state["prematch_source"] = source


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
                if len(parts) >= 3 and slug not in seen:
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

        for market in markets:
            if not market or market.get("winner"):
                continue
            threshold = SERIES_DONE_PRICE if market.get("is_series") else MAP_DONE_PRICE
            winner = infer_winner_from_market_prices(market, threshold)
            if winner:
                market["winner"] = winner
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


def infer_winner_from_market_prices(market_data, threshold):
    if not market_data or market_data.get("winner"):
        return market_data.get("winner") if market_data else None

    price1 = market_data.get("price1")
    price2 = market_data.get("price2")
    if price1 is None or price2 is None:
        return None

    if price1 > price2 and price1 >= threshold:
        return market_data.get("team1")
    if price2 > price1 and price2 >= threshold:
        return market_data.get("team2")
    return None


def is_live_market(market_data):
    if not market_data or market_data.get("winner"):
        return False
    return 5 < market_data["price1"] < 95 and 5 < market_data["price2"] < 95


def price_for_team(market_data, team):
    if not market_data or not team:
        return None
    if market_data["team1"] == team:
        return market_data["price1"]
    if market_data["team2"] == team:
        return market_data["price2"]
    return None


def other_team_in_market(market_data, team):
    if not market_data or not team:
        return None
    if market_data["team1"] == team:
        return market_data["team2"]
    if market_data["team2"] == team:
        return market_data["team1"]
    return None


def ordered_series_prices(series, first_team, second_team):
    first_price = price_for_team(series, first_team)
    second_price = price_for_team(series, second_team)
    return first_price, second_price


def format_pair_line(market, first_team=None, second_team=None):
    if not market:
        return ""
    first_team = first_team or market["team1"]
    second_team = second_team or market["team2"]
    first_price = price_for_team(market, first_team)
    second_price = price_for_team(market, second_team)
    if first_price is None or second_price is None:
        return f"{market['team1']} {market['price1']}¢ / {market['team2']} {market['price2']}¢"
    return f"{first_team} {first_price}¢ / {second_team} {second_price}¢"


def is_map_winner_market(market_type, question, group_title):
    haystack = f"{question} {group_title}".lower()
    if market_type != "child_moneyline" and "winner" not in haystack:
        return False
    blocked_words = ["total", "over/under", "handicap", "round"]
    return not any(word in haystack for word in blocked_words)


def map_number_from_market_text(question, group_title, slug):
    haystack = f"{question} {group_title} {slug}".lower()
    for number in (1, 2, 3):
        patterns = [
            f"map {number} winner",
            f"map{number} winner",
            f"map-{number}-winner",
            f"map_{number}_winner",
            f"game {number} winner",
            f"game{number} winner",
            f"game-{number}-winner",
            f"game_{number}_winner",
        ]
        if any(pattern in haystack for pattern in patterns) or re.search(
            rf"\b(?:map|game)[\s_-]*{number}\b|\b{number}(?:st|nd|rd)?[\s_-]*map\b|\bk{number}\b",
            haystack,
        ):
            return number
    return None


def map2_result(data):
    map1 = data.get("map1")
    map2 = data.get("map2")
    if not map1 or not map2:
        return None
    map1_winner = map1.get("winner")
    map2_winner = map2.get("winner")
    if not map1_winner or not map2_winner:
        return None
    return "sweep" if map1_winner == map2_winner else "split"


def forecast_split_prices(series, map1_winner, split_winner, state):
    if not series or not map1_winner or not split_winner:
        return None

    prematch_fav = state.get("prematch_fav")
    prematch_fav_price = state.get("prematch_fav_price")
    if not prematch_fav or not prematch_fav_price:
        return None

    _, band_data = get_band(prematch_fav_price)
    if not band_data:
        return None

    if map1_winner == prematch_fav and split_winner != prematch_fav:
        delta = band_data["fav_m1_dog_m2_delta"]
        note = "фаворит взяв К1, інша команда зрівнює 1:1"
    elif map1_winner != prematch_fav and split_winner == prematch_fav:
        delta = band_data["dog_m1_fav_m2_delta"]
        note = "андердог взяв К1, фаворит зрівнює 1:1"
    else:
        return None

    fav_forecast = max(0.1, min(99.9, round(prematch_fav_price + delta, 1)))
    if float(fav_forecast).is_integer():
        fav_forecast = int(fav_forecast)

    other = other_team_in_market(series, prematch_fav)
    other_forecast = round(100 - fav_forecast, 1)
    if float(other_forecast).is_integer():
        other_forecast = int(other_forecast)

    return {
        prematch_fav: fav_forecast,
        other: other_forecast,
        "note": note,
        "delta": delta,
    }


def series_forecast_after_map(series, map1_winner, map_winner, state):
    if not series or not map1_winner or not map_winner:
        return None

    other = other_team_in_market(series, map_winner)
    if not other:
        return None

    if map_winner == map1_winner:
        return {
            map_winner: 100,
            other: 0,
            "kind": "2:0 sweep",
        }

    forecast = forecast_split_prices(series, map1_winner, map_winner, state)
    if forecast:
        forecast["kind"] = "1:1 split"
    return forecast


def verdict_details(series, map1, band_range):
    reasons = []
    if not series or series["volume"] < MIN_SERIES_LIQUIDITY:
        volume = series["volume"] if series else 0
        reasons.append(f"об'єм серії ${volume:,.0f} < ${MIN_SERIES_LIQUIDITY:,.0f}")

    return not reasons, reasons


def format_map_prices(data):
    lines = []
    markets = [("map1", "К1"), ("map2", "К2")]
    for key, label in markets:
        market = data.get(key)
        if not market:
            continue
        lines.append(
            f"🗺 <b>{label}:</b> {format_pair_line(market)}"
        )
    return "\n".join(lines)


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
        slug = (m.get("slug") or "").lower()
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
            "is_series": is_series_market,
        }
        if is_series_market:
            result["series"] = md
        elif is_map_winner_market(market_type, question, group_title):
            map_number = map_number_from_market_text(question, group_title, slug)
            if map_number == 1:
                result["map1"] = md
            elif map_number == 2:
                result["map2"] = md
            elif map_number == 3:
                result["map3"] = md
    return result


def get_match_stage(data):
    series = data.get("series")
    map1 = data.get("map1")
    map2 = data.get("map2")
    map3 = data.get("map3")
    if not series:
        return "unknown"

    map1_winner = map1.get("winner") if map1 else None
    map2_winner = map2.get("winner") if map2 else None

    if map2_winner:
        if map1_winner and map1_winner == map2_winner:
            return "map2_done_sweep"
        if map1_winner and map1_winner != map2_winner:
            if is_live_market(map3):
                return "map3_live"
            return "map2_done_split"
        return "map2_done_sweep" if max(series["price1"], series["price2"]) >= 90 else "map2_done_split"

    if map3 and map3.get("winner"):
        return "finished"

    if series.get("winner"):
        return "finished"

    if map1_winner:
        if is_live_market(map2):
            return "map2_live"
        return "map1_done"

    if not has_started(data.get("start_date", "")):
        return "before"

    if map1 and not is_live_market(map1) and is_live_market(map2):
        return "map2_live"

    if is_live_market(map1):
        return "map1_live"

    return "before"


# ============================================================
# ПОВІДОМЛЕННЯ 1 — Нагадування за 30 хвилин
# ============================================================
async def msg2_reminder(slug):
    event, data = await get_parsed_event(slug)
    if not event:
        return False
    series = data.get("series")
    map1 = data.get("map1")
    title = data.get("title", slug)
    if not series:
        return False
    if series["volume"] < MIN_SERIES_LIQUIDITY:
        return False
    has_map_markets = bool(data.get("map1") or data.get("map2") or data.get("map3"))
    capture_prematch_snapshot(slug, data, source="reminder", force=True)

    fav_price = max(series["price1"], series["price2"])
    band_range, _ = get_band(fav_price)
    liq_ok, reasons = verdict_details(series, map1, band_range)

    msg = f"""⏰ <b>МАТЧ ЧЕРЕЗ 30 ХВИЛИН!</b>
⚔️ <b>{title}</b>

📊 <b>Серія:</b> {format_pair_line(series)}
💧 Об'єм серії: ${series['volume']:,.0f}"""

    map_lines = format_map_prices(data)
    if map_lines:
        msg += f"\n{map_lines}"

    if liq_ok and has_map_markets:
        msg += "\n\n✅ <b>Вердикт:</b> Кандидат по об'єму серії — чекаємо завершення К1"
    elif liq_ok:
        msg += "\n\n✅ <b>Вердикт:</b> Кандидат по об'єму серії — чекаю, чи підтягнуться карти K1/K2"
    else:
        msg += "\n\n⚠️ <b>Вердикт:</b> Не кандидат"
        msg += "\nПричини: " + "; ".join(reasons)

    if has_map_markets:
        msg += "\n\n📌 Прогноз і числа для калькулятора прийдуть після К1."
    else:
        msg += "\n\n📌 Калькулятор буде тільки якщо Polymarket підтягне ринки K1/K2."

    await send_telegram(msg)
    print(f"[2] {title}")
    return True


async def maybe_send_due_reminder(slug, data=None):
    state = match_states.setdefault(slug, {})
    if state.get("notified_reminder"):
        return

    start_date = (data or {}).get("start_date") or state.get("start_date", "")
    if not start_date:
        return

    secs = seconds_until_start(start_date)
    if secs > REMINDER_SECONDS_BEFORE_START or secs < -REMINDER_LATE_GRACE_SECONDS:
        return

    stage = state.get("stage") or (get_match_stage(data) if data else "")
    if stage not in ["before", "map1_live"]:
        return

    sent = await msg2_reminder(slug)
    if sent:
        state["notified_reminder"] = True


async def schedule_reminder(slug, start_date_str):
    if not start_date_str:
        return
    secs = seconds_until_start(start_date_str)
    delay = secs - REMINDER_SECONDS_BEFORE_START
    if delay > 0:
        await asyncio.sleep(delay)
    state = match_states.setdefault(slug, {})
    if state.get("stage") in ["before", "map1_live"] and not state.get("notified_reminder"):
        sent = await msg2_reminder(slug)
        if sent:
            state["notified_reminder"] = True


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

    state = match_states.setdefault(slug, {})
    state["strategy_active"] = False
    winner_k1 = map1.get("winner") if map1 else None
    state["map1_winner"] = winner_k1

    prematch_fav = state.get("prematch_fav") or (
        series["team1"] if series["price1"] >= series["price2"] else series["team2"]
    )
    prematch_fav_price = state.get("prematch_fav_price") or max(series["price1"], series["price2"])
    prematch_source = state.get("prematch_source") or "first_seen"
    prematch_reliable = bool(prematch_fav and prematch_fav_price)
    band_range, band_data = get_band(prematch_fav_price)
    calc_market = map2 if map2 and not map2.get("winner") else None
    calc_label = "К2"

    msg = f"""🎮 <b>К1 ЗАКІНЧИЛАСЬ!</b>
⚔️ {title}
🏆 К1 взяв: <b>{winner_k1 or "невідомо"}</b> | 1:0

📈 Серія зараз: {format_pair_line(series)}
"""

    if calc_market:
        msg += f"🗺 {calc_label}: {format_pair_line(calc_market)}\n"

    if calc_market:
        if not winner_k1:
            msg += f"""

⚠️ <b>Калькулятор автоматично не заповнюємо.</b>
Бот бачить, що наступний ринок уже активний, але не зміг точно визначити, хто взяв К1.
Для цієї стратегії переможець К1 обов'язковий — перевір K1 вручну перед входом."""
        elif not prematch_reliable:
            msg += f"""

⚠️ <b>Калькулятор автоматично не заповнюємо.</b>
Недостатньо даних для фаворита серії — перевір матч вручну."""
        elif prematch_fav not in (calc_market["team1"], calc_market["team2"]):
            msg += f"""

⚠️ <b>Калькулятор автоматично не заповнюємо.</b>
Прематчевий фаворит <b>{prematch_fav}</b> не знайдений у маркеті {calc_label}.
Потрібна ручна перевірка назв команд."""
        else:
            team_a = prematch_fav
            team_b = other_team_in_market(calc_market, team_a) or calc_market["team2"]
            s1, s2 = ordered_series_prices(series, team_a, team_b)
            team_a_map = price_for_team(calc_market, team_a)
            team_b_map = price_for_team(calc_market, team_b)
            scenario_a = series_forecast_after_map(series, winner_k1, team_a, state)
            scenario_b = series_forecast_after_map(series, winner_k1, team_b, state)
            split_winner = other_team_in_market(calc_market, winner_k1)
            split_forecast = forecast_split_prices(series, winner_k1, split_winner, state) if split_winner else None
            fav_forecast = split_forecast.get(prematch_fav) if split_forecast else None
            band_text = f"{band_range[0]}-{band_range[1]}%" if band_range else "поза базою"
            if (
                winner_k1
                and team_a_map is not None
                and team_b_map is not None
                and s1 is not None
                and s2 is not None
                and scenario_a
                and scenario_b
                and split_forecast
            ):
                state["strategy_active"] = True

            msg += f"""

🤖 Прогноз (база {band_text}, прематч фаворит: {prematch_fav} {prematch_fav_price}¢):"""
            if prematch_source not in {"before", "reminder"}:
                msg += f"""
  Джерело фаворита: перший знімок бота ({prematch_source})"""
            if split_forecast and fav_forecast is not None:
                delta = split_forecast.get("delta")
                delta_text = f" ({delta:+g}¢ від бази)" if delta is not None else ""
                msg += f"""
  При 1:1 серія {prematch_fav} → ~{fav_forecast}¢{delta_text}"""
            else:
                msg += """
  При 1:1 прогноз серії треба звірити в базі вручну"""

            msg += f"""

📋 <b>ЩО ВПИСАТИ В КАЛЬКУЛЯТОР:</b>
Команда A (прематч фаворит): <b>{team_a}</b>
Команда A · Матч ({calc_label}): <b>{team_a_map if team_a_map is not None else "?"}</b>
Команда A · Серія: <b>{s1 if s1 is not None else "?"}</b>
Команда B (друга команда): <b>{team_b}</b>
Команда B · Матч ({calc_label}): <b>{team_b_map if team_b_map is not None else "?"}</b>
Команда B · Серія: <b>{s2 if s2 is not None else "?"}</b>

⚠️ <b>Не плутай ноги:</b>
Матч = саме ринок {calc_label} Winner.
Серія = переможець серії / Match Winner.
Купуй тільки зелену RECOMMENDED конструкцію з калькулятора."""

            if scenario_a:
                msg += f"""

Якщо <b>{team_a}</b> бере {calc_label} ({scenario_a.get("kind", "?")}):
  {team_a} серія → <b>{scenario_a.get(team_a, "?")}</b>
  {team_b} серія → <b>{scenario_a.get(team_b, "?")}</b>"""
            else:
                msg += f"""

Якщо <b>{team_a}</b> бере {calc_label}:
  прогноз серії треба звірити вручну"""

            if scenario_b:
                msg += f"""

Якщо <b>{team_b}</b> бере {calc_label} ({scenario_b.get("kind", "?")}):
  {team_a} серія → <b>{scenario_b.get(team_a, "?")}</b>
  {team_b} серія → <b>{scenario_b.get(team_b, "?")}</b>"""
            else:
                msg += f"""

Якщо <b>{team_b}</b> бере {calc_label}:
  прогноз серії треба звірити вручну"""
    else:
        msg += "\n⚠️ К2 Winner market не знайдено — калькулятор поки не заповнюємо."

    msg += "\n\n⚡️ У тебе ~5 хвилин!"
    await send_telegram(msg)
    print(f"[3] К1 done: {title}")


# ============================================================
# ПОВІДОМЛЕННЯ 4a — Sweep 2:0
# ============================================================
async def msg4a_sweep(slug, data, action_ok=False):
    series = data.get("series")
    title = data.get("title", slug)
    if not series:
        return
    winner = series.get("winner") or (series["team1"] if series["price1"] > series["price2"] else series["team2"])
    msg = (
        f"🏆 <b>К2 ЗАКІНЧИЛАСЬ! 2:0 SWEEP</b>\n"
        f"⚔️ {title}\n"
        f"🥇 Переможець серії: <b>{winner}</b>\n\n"
    )
    if action_ok:
        msg += "✅ Обидві ноги резолвляться автоматично\n💰 Профіт зарахується на рахунок"
    else:
        msg += "ℹ️ K1-калькулятор не був активований ботом, тому це тільки інформаційне повідомлення."

    await send_telegram(msg)
    print(f"[4a] Sweep: {title}")


# ============================================================
# ПОВІДОМЛЕННЯ 4b — Split 1:1
# ============================================================
async def msg4b_split(slug, data, action_ok=False):
    series = data.get("series")
    map2 = data.get("map2")
    title = data.get("title", slug)
    if not series:
        return

    map2_winner = None
    if map2:
        map2_winner = map2.get("winner")

    msg = f"""⚖️ <b>К2 ЗАКІНЧИЛАСЬ! РАХУНОК 1:1</b>
⚔️ {title}
🗺 К2 взяв: <b>{map2_winner or "невідомо"}</b>

📈 Серія зараз: {format_pair_line(series)}"""

    if action_ok:
        msg += """

✅ <b>Що робити:</b>
Нога «К2» резолвиться в 100¢ → профіт
<b>Продай ногу «СЕРІЯ» зараз по поточній ціні!</b>
Фіксуй гарантований профіт зараз."""
    else:
        msg += """

ℹ️ K1-калькулятор не був активований ботом, тому це тільки інформаційне повідомлення."""

    await send_telegram(msg)
    print(f"[4b] Split 1:1: {title}")

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
                if series["volume"] < MIN_SERIES_LIQUIDITY:
                    print(f"[SKIP] Об'єм серії ${series['volume']:,.0f} < ${MIN_SERIES_LIQUIDITY:,.0f}: {data['title']}")
                    continue

                stage = get_match_stage(data)

                # Пропускаємо вже завершені
                if stage == "finished":
                    notified_slugs.add(slug)
                    print(f"[SKIP] Завершений: {slug}")
                    continue

                snapshot_source = "before" if stage == "before" else ("pre_k1" if stage == "map1_live" else "first_seen")
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
                    "prematch_fav": series["team1"] if series["price1"] >= series["price2"] else series["team2"],
                    "prematch_fav_price": max(series["price1"], series["price2"]),
                    "prematch_dog": series["team2"] if series["price1"] >= series["price2"] else series["team1"],
                    "prematch_dog_price": min(series["price1"], series["price2"]),
                    "prematch_source": snapshot_source,
                    "strategy_active": False,
                }
                capture_prematch_snapshot(slug, data, source=snapshot_source)
                notified_slugs.add(slug)

                if stage == "before":
                    if seconds_until_start(data.get("start_date", "")) <= REMINDER_SECONDS_BEFORE_START:
                        await maybe_send_due_reminder(slug, data)
                    else:
                        task = asyncio.create_task(
                            schedule_reminder(slug, data.get("start_date", ""))
                        )
                        reminder_tasks[slug] = task
                elif stage == "map1_live" and not match_states[slug]["notified_reminder"]:
                    await maybe_send_due_reminder(slug, data)

                # Якщо бот уперше побачив матч уже після К1/К2, не стрибаємо
                # одразу в повідомлення про К2 без контексту.
                if stage in ["map1_done", "map2_live"] \
                        and not match_states[slug]["notified_map1"]:
                    match_states[slug]["notified_map1"] = True
                    await msg3_map1_done(slug, data)

                strategy_active = match_states[slug].get("strategy_active")

                if stage == "map2_done_sweep" and not match_states[slug]["notified_map2"]:
                    match_states[slug]["notified_map2"] = True
                    match_states[slug]["notified_final"] = True
                    match_states[slug]["was_sweep"] = True
                    await msg4a_sweep(slug, data, action_ok=strategy_active)

                if stage in ["map2_done_split", "map3_live"] \
                        and map2_result(data) == "split" \
                        and not match_states[slug]["notified_map2"]:
                    match_states[slug]["notified_map2"] = True
                    match_states[slug]["notified_final"] = True
                    await msg4b_split(slug, data, action_ok=strategy_active)

        except Exception as e:
            print(f"Помилка scan: {e}")

        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


# ============================================================
# ВІДСТЕЖЕННЯ АКТИВНИХ МАТЧІВ
# ============================================================
async def check_active_matches():
    await asyncio.sleep(ACTIVE_CHECK_INITIAL_DELAY_SECONDS)

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
                await maybe_send_due_reminder(slug, data)
                strategy_active = state.get("strategy_active")

                # К1 закінчилась
                if stage in ["map1_done", "map2_live"] \
                        and prev_stage in ["before", "map1_live"] \
                        and not state["notified_map1"]:
                    state["notified_map1"] = True
                    await msg3_map1_done(slug, data)

                # Sweep 2:0
                if stage == "map2_done_sweep" and not state["notified_map2"]:
                    state["notified_map2"] = True
                    state["notified_final"] = True
                    state["was_sweep"] = True
                    await msg4a_sweep(slug, data, action_ok=strategy_active)

                # Split 1:1
                if stage in ["map2_done_split", "map3_live"] \
                        and map2_result(data) == "split" \
                        and not state["notified_map2"]:
                    state["notified_map2"] = True
                    state["notified_final"] = True
                    await msg4b_split(slug, data, action_ok=strategy_active)

        except Exception as e:
            print(f"Помилка check: {e}")

        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


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
        "⏰ 1 — нагадування за 30 хв\n"
        "🎮 2 — після К1 + калькулятор\n"
        "🏆 3 — після К2 (sweep або 1:1)"
    )
    await asyncio.gather(
        scan_matches(),
        check_active_matches(),
    )


if __name__ == "__main__":
    asyncio.run(main())
