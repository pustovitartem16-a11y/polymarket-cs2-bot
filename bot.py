import asyncio
import json
import websockets
import httpx
import os

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
WS_URL = "wss://sports-api.polymarket.com/ws"
GAMMA_API = "https://gamma-api.polymarket.com"

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
match_states = {}


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
        print(f"Ошибка отправки: {e}")


async def get_event_data(home, away):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for search in [home, away]:
                r = await client.get(f"{GAMMA_API}/events", params={
                    "search": search,
                    "active": "true",
                    "limit": 10,
                })
                events = r.json()
                if not isinstance(events, list):
                    continue
                for event in events:
                    title = event.get("title", "").lower()
                    if (home.lower()[:4] in title or home.lower() in title) and \
                       (away.lower()[:4] in title or away.lower() in title):
                        return parse_markets(event)
    except Exception as e:
        print(f"Ошибка get_event_data({home} vs {away}): {e}")
    return None


def parse_markets(event):
    markets = event.get("markets", [])
    result = {
        "title": event.get("title", ""),
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


async def send_preview(home, away):
    data = await get_event_data(home, away)

    if not data or not data.get("series"):
        await send_telegram(
            f"📋 <b>НОВЫЙ МАТЧ</b>\n"
            f"⚔️ <b>{home} vs {away}</b>\n\n"
            f"⚠️ Данные на Polymarket не найдены — проверь вручную"
        )
        return

    series = data["series"]
    map1 = data.get("map1")

    if series["volume"] < MIN_SERIES_LIQUIDITY:
        await send_telegram(
            f"📋 <b>НОВЫЙ МАТЧ</b>\n"
            f"⚔️ <b>{home} vs {away}</b>\n"
            f"⚠️ Мало ликвидности (${series['volume']:,.0f}) — пропускаем"
        )
        return

    fav_price = max(series["price1"], series["price2"])
    band_range, band_data = get_band(fav_price)
    map1_liq = map1["volume"] if map1 else 0
    liq_ok = map1_liq >= 3000 and band_range and band_range[0] <= 70
    liq_icon = "✅" if liq_ok else "⚠️"
    verdict = "Кандидат для торговли" if liq_ok else "Пропустить"

    msg = f"""📋 <b>НОВЫЙ МАТЧ ОБНАРУЖЕН</b>
⚔️ <b>{home} vs {away}</b>

📊 <b>Серия:</b> {series['team1']} {series['price1']}¢ / {series['team2']} {series['price2']}¢
💧 Объём серии: ${series['volume']:,.0f}"""

    if map1:
        msg += f"""
🗺 <b>К1:</b> {map1['team1']} {map1['price1']}¢ / {map1['team2']} {map1['price2']}¢
💧 Объём К1: ${map1_liq:,.0f}"""

    if band_range and band_data:
        msg += f"""

📉 <b>Диапазон фаворита:</b> {band_range[0]}-{band_range[1]}%
   Фав берёт К1: серия +{band_data['fav_win']}
   Аут берёт К1: серия {band_data['dog_win']}"""

    msg += f"\n\n{liq_icon} <b>Вердикт:</b> {verdict}"
    await send_telegram(msg)
    print(f"[ПРЕВЬЮ] {home} vs {away}")


async def send_start_reminder(home, away):
    data = await get_event_data(home, away)
    series = data.get("series") if data else None

    if series and series["volume"] >= MIN_SERIES_LIQUIDITY:
        fav_price = max(series["price1"], series["price2"])
        band_range, _ = get_band(fav_price)
        band_str = f"{band_range[0]}-{band_range[1]}%" if band_range else "—"
        msg = f"""⚡️ <b>МАТЧ НАЧИНАЕТСЯ!</b>
⚔️ <b>{home} vs {away}</b>

📊 Серия: {series['team1']} {series['price1']}¢ / {series['team2']} {series['price2']}¢
💧 Объём: ${series['volume']:,.0f}
📉 Диапазон: {band_str}

👁 Следи за К1!"""
    else:
        msg = f"""⚡️ <b>МАТЧ НАЧИНАЕТСЯ!</b>
⚔️ <b>{home} vs {away}</b>
👁 Следи за К1!"""

    await send_telegram(msg)
    print(f"[СТАРТ] {home} vs {away}")


async def handle_map1_end(slug, score, home, away):
    data = await get_event_data(home, away)
    series = data.get("series") if data else None
    map2 = data.get("map2") if data else None
    title = (data.get("title") if data else None) or f"{home} vs {away}"

    try:
        parts = score.split("|")
        home_maps, away_maps = parts[1].split("-")
        winner_is_home = int(home_maps) > int(away_maps)
    except Exception:
        winner_is_home = True

    winner = home if winner_is_home else away
    match_states[slug]["map1_winner_home"] = winner_is_home

    if not series or series["volume"] < MIN_SERIES_LIQUIDITY:
        await send_telegram(
            f"ℹ️ <b>К1 закончилась:</b> {home} vs {away}\n"
            f"🏆 К1 взял: <b>{winner}</b>\n"
            f"⚠️ Мало ликвидности — пропускаем"
        )
        return

    fav_price = max(series["price1"], series["price2"])
    if series["price1"] >= series["price2"]:
        cur_fav, cur_fav_p = series["team1"], series["price1"]
        cur_dog, cur_dog_p = series["team2"], series["price2"]
    else:
        cur_fav, cur_fav_p = series["team2"], series["price2"]
        cur_dog, cur_dog_p = series["team1"], series["price1"]

    band_range, band_data = get_band(cur_fav_p)
    revert = band_data["revert"] if band_data else "~50"

    msg = f"""🎮 <b>К1 ЗАКОНЧИЛАСЬ!</b>
⚔️ <b>{title}</b>
🏆 Взял К1: <b>{winner}</b> | Счёт: {score}

📈 <b>Серия сейчас:</b>
  {cur_fav} {cur_fav_p}¢ / {cur_dog} {cur_dog_p}¢
  Объём: ${series['volume']:,.0f}"""

    if map2:
        msg += f"""

🗺 <b>К2:</b>
  {map2['team1']} {map2['price1']}¢ / {map2['team2']} {map2['price2']}¢
  Объём: ${map2['volume']:,.0f}"""

    if band_data:
        msg += f"""

🤖 <b>Прогноз (база {band_range[0]}-{band_range[1]}%):</b>
  При 1:1 серия {cur_fav} → ~{revert}¢"""

    msg += "\n\n⚡️ <b>У тебя ~5 минут — открывай калькулятор!</b>"
    await send_telegram(msg)
    print(f"[К1] {home} vs {away}: {winner}")


async def handle_map2_end(slug, score, home, away):
    data = await get_event_data(home, away)
    series = data.get("series") if data else None
    map3 = data.get("map3") if data else None
    title = (data.get("title") if data else None) or f"{home} vs {away}"

    if not series or series["volume"] < MIN_SERIES_LIQUIDITY:
        return

    try:
        parts = score.split("|")
        home_maps, away_maps = parts[1].split("-")
        home_maps, away_maps = int(home_maps), int(away_maps)
    except Exception:
        return

    if home_maps == 2 or away_maps == 2:
        winner = home if home_maps == 2 else away
        msg = f"""🏆 <b>СЕРИЯ ЗАКОНЧИЛАСЬ! 2:0 SWEEP</b>
⚔️ <b>{title}</b>
🥇 Победитель: <b>{winner}</b>

✅ Обе ноги резолвятся автоматически
💰 Профит зачислится на счёт Polymarket"""

    elif home_maps == 1 and away_maps == 1:
        map1_winner_home = match_states.get(slug, {}).get("map1_winner_home")
        if map1_winner_home is True:
            map2_winner = away
        elif map1_winner_home is False:
            map2_winner = home
        else:
            map2_winner = "неизвестно"

        msg = f"""⚖️ <b>К2 ЗАКОНЧИЛАСЬ! СЧЁТ 1:1</b>
⚔️ <b>{title}</b>
🗺 К2 взял: <b>{map2_winner}</b>

📈 <b>Серия сейчас:</b>
  {series['team1']} {series['price1']}¢ / {series['team2']} {series['price2']}¢"""

        if map3:
            msg += f"""
🗺 <b>К3:</b>
  {map3['team1']} {map3['price1']}¢ / {map3['team2']} {map3['price2']}¢"""

        msg += f"""

✅ <b>Что делать:</b>
Нога «К2» резолвится в 100¢ → профит зафиксирован
<b>Продай ногу «СЕРИЯ»</b> по текущей цене прямо сейчас!
Не жди К3 — фиксируй гарантированный профит

⚡️ Открывай Polymarket и продавай серию ногу!"""
    else:
        return

    await send_telegram(msg)
    print(f"[К2] {home} vs {away}: {home_maps}:{away_maps}")


async def handle_series_end(slug, score, home, away):
    try:
        parts = score.split("|")
        home_maps, away_maps = parts[1].split("-")
        home_maps, away_maps = int(home_maps), int(away_maps)
        if home_maps + away_maps < 3:
            return
        winner = home if home_maps > away_maps else away
    except Exception:
        return

    await send_telegram(
        f"🏁 <b>СЕРИЯ ЗАВЕРШЕНА (К3)</b>\n"
        f"⚔️ <b>{home} vs {away}</b>\n"
        f"🥇 Победитель: <b>{winner}</b>\n"
        f"📊 Счёт карт: {home_maps}:{away_maps}\n\n"
        f"✅ Серия нога резолвится автоматически"
    )
    print(f"[К3] {home} vs {away}: {winner}")


async def handle_ws_message(data):
    if data.get("leagueAbbreviation") != "cs2":
        return

    slug = data.get("slug", "")
    period = data.get("period", "")
    status = data.get("status", "")
    score = data.get("score", "")
    home = data.get("homeTeam", "")
    away = data.get("awayTeam", "")

    if not slug or not home or not away:
        return

    # Инициализируем состояние при первом появлении
    if slug not in match_states:
        match_states[slug] = {
            "period": None,
            "status": None,
            "notified_preview": False,
            "notified_start": False,
            "notified_map1": False,
            "notified_map2": False,
            "notified_final": False,
            "map1_winner_home": None,
        }

    prev_status = match_states[slug]["status"]
    prev_period = match_states[slug]["period"]
    match_states[slug]["status"] = status
    match_states[slug]["period"] = period

    # Превью — при любом первом появлении not_started
    if status == "not_started" and not match_states[slug]["notified_preview"]:
        match_states[slug]["notified_preview"] = True
        asyncio.create_task(send_preview(home, away))

    # Напоминание при старте
    if status == "running" and prev_status != "running" and not match_states[slug]["notified_start"]:
        match_states[slug]["notified_start"] = True
        asyncio.create_task(send_start_reminder(home, away))

    # К1 закончилась
    if prev_period == "1/3" and period == "2/3":
        if not match_states[slug]["notified_map1"]:
            match_states[slug]["notified_map1"] = True
            asyncio.create_task(handle_map1_end(slug, score, home, away))

    # К2 закончилась
    elif prev_period == "2/3" and period in ["3/3", "finished"]:
        if not match_states[slug]["notified_map2"]:
            match_states[slug]["notified_map2"] = True
            asyncio.create_task(handle_map2_end(slug, score, home, away))

    # Серия завершена
    elif status == "finished" and not match_states[slug]["notified_final"]:
        match_states[slug]["notified_final"] = True
        asyncio.create_task(handle_series_end(slug, score, home, away))


async def websocket_listener():
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=None) as ws:
                print("WebSocket подключён")
                async for message in ws:
                    if message == "ping":
                        await ws.send("pong")
                        continue
                    try:
                        data = json.loads(message)
                        await handle_ws_message(data)
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            print(f"WebSocket ошибка: {e}, переподключение через 5с...")
            await asyncio.sleep(5)


async def main():
    print("Бот запущен...")
    await send_telegram(
        "✅ <b>Polymarket CS2 бот запущен!</b>\n\n"
        "Буду присылать:\n"
        "• При появлении матча — превью\n"
        "• При старте матча — напоминание\n"
        "• После К1 — цены и прогноз\n"
        "• После К2 — что делать с позицией\n"
        "• После К3 — итог серии"
    )
    await websocket_listener()


if __name__ == "__main__":
    asyncio.run(main())
