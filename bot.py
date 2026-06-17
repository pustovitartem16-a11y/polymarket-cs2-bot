import asyncio
import json
import websockets
import httpx
import os
from datetime import datetime, timezone

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
WS_URL = "wss://sports-api.polymarket.com/ws"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

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

MIN_SERIES_LIQUIDITY = 50_000
MIN_MAP_LIQUIDITY = 3_000

match_states = {}
preview_tasks = {}


def get_band(fav_price):
    for (lo, hi), data in BAND_SHIFTS.items():
        if lo <= fav_price < hi:
            return (lo, hi), data
    return None, None


async def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        })


async def get_event_data(slug):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{GAMMA_API}/events", params={"slug": slug, "limit": 1})
            events = r.json()
            if not events:
                r2 = await client.get(f"{GAMMA_API}/events", params={"search": slug.replace("cs2-", "").replace("-", " "), "limit": 5})
                events = r2.json()
                if not events:
                    return None

            event = events[0]
            markets = event.get("markets", [])
            title = event.get("title", slug)
            start_date = event.get("startDate", "")

            result = {
                "title": title,
                "start_date": start_date,
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

                market_data = {
                    "team1": outcomes_list[0],
                    "team2": outcomes_list[1],
                    "price1": round(prices_float[0] * 100),
                    "price2": round(prices_float[1] * 100),
                    "volume": volume,
                }

                if ("series" in question or "winner" in question) and "map" not in question:
                    result["series"] = market_data
                elif "map 1" in question or "map1" in question:
                    result["map1"] = market_data
                elif "map 2" in question or "map2" in question:
                    result["map2"] = market_data
                elif "map 3" in question or "map3" in question:
                    result["map3"] = market_data

            return result

    except Exception as e:
        print(f"Ошибка get_event_data({slug}): {e}")
        return None


def liquidity_ok(data):
    if not data:
        return False
    series = data.get("series")
    if not series or series["volume"] < MIN_SERIES_LIQUIDITY:
        return False
    return True


def get_winner_from_score(score, home, away, map_num):
    """Определяет победителя конкретной карты из score"""
    try:
        # Формат score: "000-000|1-0|Bo3" или "000-000|1-1|Bo3"
        parts = score.split("|")
        if len(parts) >= 2:
            map_score = parts[1]  # "1-0", "1-1", "2-0" итд
            home_maps, away_maps = map_score.split("-")
            home_maps = int(home_maps)
            away_maps = int(away_maps)

            if map_num == 1:
                # К1 победитель — у кого больше карт после К1
                if home_maps > away_maps:
                    return home, True
                else:
                    return away, False
            elif map_num == 2:
                # К2 результат — смотрим общий счёт
                total = home_maps + away_maps
                if total == 2:  # 2-0 sweep
                    if home_maps == 2:
                        return home, True  # home выиграл серию 2:0
                    else:
                        return away, False
                elif total == 2 and home_maps == 1:  # 1-1
                    return None, None  # ничья, К3 впереди
    except Exception:
        pass
    return None, None


async def format_map2_result(data, slug, score, home, away):
    """Формирует уведомление после К2"""
    title = data.get("title", f"{home} vs {away}")
    series = data.get("series")

    if not series:
        return None

    try:
        parts = score.split("|")
        map_score = parts[1] if len(parts) >= 2 else "0-0"
        home_maps, away_maps = map_score.split("-")
        home_maps = int(home_maps)
        away_maps = int(away_maps)
        total_maps = home_maps + away_maps
    except Exception:
        return None

    # 2:0 sweep — серия закончилась
    if total_maps == 2 and (home_maps == 2 or away_maps == 2):
        winner = home if home_maps == 2 else away
        msg = f"""🏆 <b>СЕРИЯ ЗАКОНЧИЛАСЬ! 2:0 SWEEP</b>
⚔️ <b>{title}</b>
🥇 Победитель: <b>{winner}</b>

✅ <b>Что делать:</b>
Нога «{winner} серия» резолвится в 100¢ автоматически
Нога «К2» резолвится в 0¢ автоматически
💰 Профит зачислится на счёт Polymarket"""

    # 1:1 — К3 впереди
    elif home_maps == 1 and away_maps == 1:
        map3 = data.get("map3")
        cur_series = series

        # Определяем кто выиграл К2
        # Если после К1 счёт был 1:0 в пользу home, а сейчас 1:1 — away взял К2
        state = match_states.get(slug, {})
        map1_winner_home = state.get("map1_winner_home")

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
  {cur_series['team1']} {cur_series['price1']}¢ / {cur_series['team2']} {cur_series['price2']}¢"""

        if map3:
            msg += f"""
🗺 <b>К3:</b>
  {map3['team1']} {map3['price1']}¢ / {map3['team2']} {map3['price2']}¢"""

        msg += f"""

✅ <b>Что делать с позицией:</b>
Нога «К2» резолвится в 100¢ → профит зафиксирован
<b>Продай ногу «СЕРИЯ»</b> по текущей цене прямо сейчас
Не жди К3 — зафиксируй гарантированный профит

⚡️ Открывай Polymarket и продавай серию ногу!"""

    else:
        return None

    return msg


async def format_preview(data, slug):
    series = data.get("series")
    map1 = data.get("map1")
    title = data.get("title", slug)

    if not series:
        return None

    if series["price1"] >= series["price2"]:
        fav_price = series["price1"]
    else:
        fav_price = series["price2"]

    band_range, band_data = get_band(fav_price)
    series_liq = series["volume"]
    map1_liq = map1["volume"] if map1 else 0
    liq_ok = series_liq >= MIN_SERIES_LIQUIDITY and map1_liq >= MIN_MAP_LIQUIDITY
    liq_icon = "✅" if liq_ok else "⚠️"
    verdict = "Кандидат для торговли" if liq_ok and band_range and band_range[0] <= 70 else "Пропустить"

    msg = f"""⏰ <b>МАТЧ ЧЕРЕЗ 30 МИНУТ</b>
⚔️ <b>{title}</b>

📊 <b>Серия:</b> {series['team1']} {series['price1']}¢ / {series['team2']} {series['price2']}¢
💧 Объём серии: ${series_liq:,.0f}"""

    if map1:
        msg += f"""
🗺 <b>К1:</b> {map1['team1']} {map1['price1']}¢ / {map1['team2']} {map1['price2']}¢
💧 Объём К1: ${map1_liq:,.0f}"""

    if band_range and band_data:
        msg += f"""

📉 <b>Диапазон фаворита:</b> {band_range[0]}-{band_range[1]}%
   Сдвиг если фав берёт К1: +{band_data['fav_win']}
   Сдвиг если аут берёт К1: {band_data['dog_win']}"""

    msg += f"""

{liq_icon} <b>Вердикт:</b> {verdict}"""

    return msg


async def format_map1_result(data, slug, score, winner_is_home, home, away):
    series = data.get("series")
    map2 = data.get("map2")
    title = data.get("title", f"{home} vs {away}")

    if not series:
        return None

    winner = home if winner_is_home else away

    if series["team1"].lower() in winner.lower() or winner.lower() in series["team1"].lower():
        cur_fav = series["team1"]
        cur_fav_price = series["price1"]
        cur_dog = series["team2"]
        cur_dog_price = series["price2"]
    else:
        cur_fav = series["team2"]
        cur_fav_price = series["price2"]
        cur_dog = series["team1"]
        cur_dog_price = series["price1"]

    band_range, band_data = get_band(cur_fav_price)
    revert = band_data["revert"] if band_data else "~50"

    msg = f"""🎮 <b>К1 ЗАКОНЧИЛАСЬ!</b>
⚔️ <b>{title}</b>
🏆 Взял К1: <b>{winner}</b> | Счёт: {score}

📈 <b>Серия сейчас:</b>
  {cur_fav} {cur_fav_price}¢ / {cur_dog} {cur_dog_price}¢
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

    return msg


async def send_preview(slug, delay_seconds):
    await asyncio.sleep(delay_seconds)
    state = match_states.get(slug, {})
    if state.get("period") not in [None, "", "not_started"]:
        return

    data = await get_event_data(slug)
    if not data:
        return
    if not liquidity_ok(data):
        return

    msg = await format_preview(data, slug)
    if msg:
        await send_telegram(msg)
        print(f"Превью отправлено: {slug}")
        match_states[slug]["notified_preview"] = True


async def handle_message(data):
    if data.get("leagueAbbreviation") != "cs2":
        return

    slug = data.get("slug", "")
    period = data.get("period", "")
    status = data.get("status", "")
    score = data.get("score", "")
    home = data.get("homeTeam", "")
    away = data.get("awayTeam", "")

    if not slug:
        return

    if slug not in match_states:
        match_states[slug] = {
            "period": None,
            "status": None,
            "notified_preview": False,
            "notified_map1": False,
            "notified_map2": False,
            "map1_winner_home": None,
        }

    prev_period = match_states[slug].get("period")
    match_states[slug]["period"] = period
    match_states[slug]["status"] = status

    # Планируем превью при появлении матча
    if status == "not_started" and match_states[slug].get("status") is None:
        if not match_states[slug]["notified_preview"] and slug not in preview_tasks:
            task = asyncio.create_task(send_preview(slug, delay_seconds=1))
            preview_tasks[slug] = task

    # К1 закончилась → К2 началась
    if prev_period == "1/3" and period == "2/3":
        if not match_states[slug]["notified_map1"]:
            match_states[slug]["notified_map1"] = True

            winner_is_home = True
            try:
                parts = score.split("|")
                if len(parts) >= 2:
                    home_maps, away_maps = parts[1].split("-")
                    winner_is_home = int(home_maps) > int(away_maps)
            except Exception:
                pass

            match_states[slug]["map1_winner_home"] = winner_is_home

            event_data = await get_event_data(slug)
            if event_data and liquidity_ok(event_data):
                msg = await format_map1_result(event_data, slug, score, winner_is_home, home, away)
                if msg:
                    await send_telegram(msg)
                    print(f"К1 уведомление: {slug}")
            elif event_data:
                winner = home if winner_is_home else away
                await send_telegram(
                    f"ℹ️ К1 закончилась: {home} vs {away}\n"
                    f"Победитель: {winner}\n"
                    f"⚠️ Мало ликвидности — пропускаем"
                )

    # К2 закончилась → либо sweep 2:0 либо 1:1 → К3
    if prev_period == "2/3" and period in ["3/3", "finished"]:
        if not match_states[slug]["notified_map2"]:
            match_states[slug]["notified_map2"] = True

            event_data = await get_event_data(slug)
            if event_data and liquidity_ok(event_data):
                msg = await format_map2_result(event_data, slug, score, home, away)
                if msg:
                    await send_telegram(msg)
                    print(f"К2 уведомление: {slug}")

    # Серия закончилась полностью (после К3)
    if status == "finished" and not match_states[slug].get("notified_final"):
        match_states[slug]["notified_final"] = True

        try:
            parts = score.split("|")
            map_score = parts[1] if len(parts) >= 2 else "0-0"
            home_maps, away_maps = map_score.split("-")
            home_maps = int(home_maps)
            away_maps = int(away_maps)
            total = home_maps + away_maps

            # Уведомляем только если была К3 (3 карты сыграно)
            if total == 3:
                winner = home if home_maps > away_maps else away
                await send_telegram(
                    f"🏁 <b>СЕРИЯ ЗАВЕРШЕНА (К3)</b>\n"
                    f"⚔️ {home} vs {away}\n"
                    f"🥇 Победитель: <b>{winner}</b>\n"
                    f"📊 Счёт карт: {home_maps}:{away_maps}\n\n"
                    f"✅ Серия нога резолвится автоматически"
                )
        except Exception:
            pass


async def schedule_previews():
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                now = datetime.now(timezone.utc)
                r = await client.get(
                    f"{GAMMA_API}/events",
                    params={
                        "tag": "cs2",
                        "active": "true",
                        "limit": 50,
                        "order": "startDate",
                        "ascending": "true",
                    }
                )
                events = r.json()

                for event in events:
                    slug = event.get("slug", "")
                    start_str = event.get("startDate", "")

                    if not slug or not start_str:
                        continue
                    if match_states.get(slug, {}).get("notified_preview"):
                        continue
                    if slug in preview_tasks:
                        continue

                    try:
                        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                        seconds_until = (start_dt - now).total_seconds()

                        if 0 < seconds_until <= 35 * 60:
                            delay = max(0, seconds_until - 30 * 60)
                            if slug not in match_states:
                                match_states[slug] = {
                                    "period": None,
                                    "status": None,
                                    "notified_preview": False,
                                    "notified_map1": False,
                                    "notified_map2": False,
                                    "notified_final": False,
                                    "map1_winner_home": None,
                                }
                            task = asyncio.create_task(send_preview(slug, delay_seconds=delay))
                            preview_tasks[slug] = task
                            print(f"Запланировано превью через {delay:.0f}с: {slug}")

                    except Exception:
                        continue

        except Exception as e:
            print(f"Ошибка schedule_previews: {e}")

        await asyncio.sleep(5 * 60)


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
                        await handle_message(data)
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
        "• Превью за 30 мин до матча\n"
        "• Уведомление после К1\n"
        "• Уведомление после К2 (что делать с позицией)\n"
        "• Уведомление после К3 (если будет)"
    )

    await asyncio.gather(
        websocket_listener(),
        schedule_previews(),
    )


if __name__ == "__main__":
    asyncio.run(main())
