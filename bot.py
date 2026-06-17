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
scheduled_slugs = set()


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


async def fetch_cs2_events():
    """Достаёт все предстоящие CS2 события через несколько методов"""
    events = []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Метод 1: поиск по тегу esports
            for tag in ["cs2", "esports", "counter-strike"]:
                try:
                    r = await client.get(f"{GAMMA_API}/events", params={
                        "tag_slug": tag,
                        "active": "true",
                        "limit": 50,
                        "order": "startDate",
                        "ascending": "true",
                    })
                    data = r.json()
                    if isinstance(data, list):
                        events.extend(data)
                except Exception:
                    pass

            # Метод 2: поиск по ключевому слову
            for keyword in ["cs2", "counter-strike", "CSGO"]:
                try:
                    r = await client.get(f"{GAMMA_API}/events", params={
                        "search": keyword,
                        "active": "true",
                        "limit": 30,
                    })
                    data = r.json()
                    if isinstance(data, list):
                        events.extend(data)
                except Exception:
                    pass

    except Exception as e:
        print(f"Ошибка fetch_cs2_events: {e}")

    # Дедупликация по slug
    seen = set()
    unique = []
    for e in events:
        slug = e.get("slug", "")
        if slug and slug not in seen:
            seen.add(slug)
            unique.append(e)

    return unique


async def get_event_markets(slug):
    """Достаёт рынки конкретного события"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{GAMMA_API}/events", params={"slug": slug, "limit": 1})
            events = r.json()
            if not events or not isinstance(events, list):
                return None
            return events[0]
    except Exception as e:
        print(f"Ошибка get_event_markets: {e}")
        return None


def parse_markets(event):
    """Парсит рынки события"""
    markets = event.get("markets", [])
    result = {"series": None, "map1": None, "map2": None, "map3": None,
              "title": event.get("title", ""), "start_date": event.get("startDate", "")}

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


async def send_preview_30(event):
    """Превью за 30 минут"""
    data = parse_markets(event)
    series = data.get("series")
    map1 = data.get("map1")
    title = data.get("title", "")

    if not series or series["volume"] < MIN_SERIES_LIQUIDITY:
        return

    fav_price = max(series["price1"], series["price2"])
    band_range, band_data = get_band(fav_price)

    map1_liq = map1["volume"] if map1 else 0
    liq_ok = map1_liq >= 3000
    liq_icon = "✅" if liq_ok else "⚠️"
    verdict = "Кандидат для торговли" if liq_ok and band_range and band_range[0] <= 70 else "Пропустить — мало маржи или ликвидности К1"

    msg = f"""⏰ <b>МАТЧ ЧЕРЕЗ 30 МИНУТ</b>
⚔️ <b>{title}</b>

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
    print(f"[30мин] Превью: {title}")


async def send_preview_5(event):
    """Напоминание за 5 минут"""
    data = parse_markets(event)
    series = data.get("series")
    title = data.get("title", "")

    if not series or series["volume"] < MIN_SERIES_LIQUIDITY:
        return

    msg = f"""⚡️ <b>МАТЧ ЧЕРЕЗ 5 МИНУТ!</b>
⚔️ <b>{title}</b>

📊 Серия: {series['team1']} {series['price1']}¢ / {series['team2']} {series['price2']}¢

👁 Открывай Polymarket и следи за К1!"""

    await send_telegram(msg)
    print(f"[5мин] Напоминание: {title}")


async def handle_map1_end(slug, score, home, away):
    """К1 закончилась"""
    event = await get_event_markets(slug)
    if not event:
        return

    data = parse_markets(event)
    series = data.get("series")
    map2 = data.get("map2")
    title = data.get("title", f"{home} vs {away}")

    if not series:
        return

    if series["volume"] < MIN_SERIES_LIQUIDITY:
        try:
            parts = score.split("|")
            winner = home if int(parts[1].split("-")[0]) > int(parts[1].split("-")[1]) else away
        except Exception:
            winner = "неизвестно"
        await send_telegram(f"ℹ️ К1: {home} vs {away} | Счёт: {score}\n⚠️ Мало ликвидности — пропускаем")
        return

    # Определяем победителя К1
    try:
        parts = score.split("|")
        home_maps, away_maps = parts[1].split("-")
        winner_is_home = int(home_maps) > int(away_maps)
    except Exception:
        winner_is_home = True

    winner = home if winner_is_home else away
    match_states[slug]["map1_winner_home"] = winner_is_home

    # Текущий фаворит по серии
    cur_fav_price = max(series["price1"], series["price2"])
    if series["price1"] >= series["price2"]:
        cur_fav = series["team1"]
        cur_dog = series["team2"]
        cur_fav_p = series["price1"]
        cur_dog_p = series["price2"]
    else:
        cur_fav = series["team2"]
        cur_dog = series["team1"]
        cur_fav_p = series["price2"]
        cur_dog_p = series["price1"]

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
    print(f"[К1] {title}: {winner}")


async def handle_map2_end(slug, score, home, away):
    """К2 закончилась"""
    event = await get_event_markets(slug)
    if not event:
        return

    data = parse_markets(event)
    series = data.get("series")
    map3 = data.get("map3")
    title = data.get("title", f"{home} vs {away}")

    if not series or series["volume"] < MIN_SERIES_LIQUIDITY:
        return

    try:
        parts = score.split("|")
        home_maps, away_maps = parts[1].split("-")
        home_maps = int(home_maps)
        away_maps = int(away_maps)
    except Exception:
        return

    # 2:0 sweep
    if home_maps == 2 or away_maps == 2:
        winner = home if home_maps == 2 else away
        msg = f"""🏆 <b>СЕРИЯ ЗАКОНЧИЛАСЬ! 2:0 SWEEP</b>
⚔️ <b>{title}</b>
🥇 Победитель: <b>{winner}</b>

✅ <b>Что делать:</b>
Обе ноги резолвятся автоматически
💰 Профит зачислится на счёт Polymarket"""

    # 1:1 — К3 впереди
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
Не жди К3 — зафиксируй гарантированный профит

⚡️ Открывай Polymarket и продавай серию ногу!"""

    else:
        return

    await send_telegram(msg)
    print(f"[К2] {title}: {home_maps}:{away_maps}")


async def handle_series_end(slug, score, home, away):
    """Серия закончилась после К3"""
    event = await get_event_markets(slug)
    title = event.get("title", f"{home} vs {away}") if event else f"{home} vs {away}"

    try:
        parts = score.split("|")
        home_maps, away_maps = parts[1].split("-")
        home_maps = int(home_maps)
        away_maps = int(away_maps)
        if home_maps + away_maps < 3:
            return
        winner = home if home_maps > away_maps else away
    except Exception:
        return

    msg = f"""🏁 <b>СЕРИЯ ЗАВЕРШЕНА (К3)</b>
⚔️ <b>{title}</b>
🥇 Победитель: <b>{winner}</b>
📊 Счёт карт: {home_maps}:{away_maps}

✅ Серия нога резолвится автоматически"""

    await send_telegram(msg)
    print(f"[К3] {title}: {winner}")


async def handle_ws_message(data):
    """Обрабатывает сообщение из Sports WebSocket"""
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
            "notified_map1": False,
            "notified_map2": False,
            "notified_final": False,
            "map1_winner_home": None,
        }

    prev_period = match_states[slug]["period"]
    match_states[slug]["period"] = period
    match_states[slug]["status"] = status

    # К1 закончилась
    if prev_period == "1/3" and period == "2/3":
        if not match_states[slug]["notified_map1"]:
            match_states[slug]["notified_map1"] = True
            await handle_map1_end(slug, score, home, away)

    # К2 закончилась
    elif prev_period == "2/3" and period in ["3/3", "finished"]:
        if not match_states[slug]["notified_map2"]:
            match_states[slug]["notified_map2"] = True
            await handle_map2_end(slug, score, home, away)

    # Серия закончилась (К3)
    elif status == "finished" and not match_states[slug]["notified_final"]:
        match_states[slug]["notified_final"] = True
        await handle_series_end(slug, score, home, away)


async def schedule_previews():
    """Каждую минуту проверяет предстоящие матчи и планирует превью"""
    while True:
        try:
            events = await fetch_cs2_events()
            now = datetime.now(timezone.utc)

            for event in events:
                slug = event.get("slug", "")
                start_str = event.get("startDate", "")
                title = event.get("title", "")

                if not slug or not start_str:
                    continue

                try:
                    start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                    seconds_until = (start_dt - now).total_seconds()
                except Exception:
                    continue

                # Превью за 30 минут
                key_30 = f"{slug}_30"
                if 25 * 60 <= seconds_until <= 35 * 60 and key_30 not in scheduled_slugs:
                    scheduled_slugs.add(key_30)
                    delay = seconds_until - 30 * 60
                    async def _send_30(e=event, d=max(0, delay)):
                        await asyncio.sleep(d)
                        await send_preview_30(e)
                    asyncio.create_task(_send_30())
                    print(f"Запланировано превью 30мин: {title}")

                # Напоминание за 5 минут
                key_5 = f"{slug}_5"
                if 2 * 60 <= seconds_until <= 8 * 60 and key_5 not in scheduled_slugs:
                    scheduled_slugs.add(key_5)
                    delay = seconds_until - 5 * 60
                    async def _send_5(e=event, d=max(0, delay)):
                        await asyncio.sleep(d)
                        await send_preview_5(e)
                    asyncio.create_task(_send_5())
                    print(f"Запланировано напоминание 5мин: {title}")

        except Exception as e:
            print(f"Ошибка schedule_previews: {e}")

        await asyncio.sleep(60)  # Проверяем каждую минуту


async def websocket_listener():
    """Слушает Sports WebSocket"""
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
        "• За 30 мин до матча — превью с коэффициентами\n"
        "• За 5 мин до матча — напоминание\n"
        "• После К1 — цены и прогноз\n"
        "• После К2 — что делать с позицией\n"
        "• После К3 — итог серии"
    )

    await asyncio.gather(
        websocket_listener(),
        schedule_previews(),
    )


if __name__ == "__main__":
    asyncio.run(main())
