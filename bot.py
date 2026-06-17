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

# Диапазоны из базы 940 матчей
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

MIN_SERIES_LIQUIDITY = 50_000   # $50K минимум серия
MIN_MAP_LIQUIDITY = 3_000       # $3K минимум К1/К2

# Храним состояния: {slug: {"period": "1/3", "notified_preview": False}}
match_states = {}
# Храним запланированные превью: {slug: task}
preview_tasks = {}


def get_band(fav_price):
    """Возвращает данные диапазона для фаворита"""
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
    """Достаёт данные события с ценами и ликвидностью"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{GAMMA_API}/events", params={"slug": slug, "limit": 1})
            events = r.json()
            if not events:
                # Попробуем поиском
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

                # Определяем тип рынка
                if ("series" in question or "winner" in question) and "map" not in question:
                    result["series"] = market_data
                elif "map 1" in question or "map1" in question:
                    result["map1"] = market_data
                elif "map 2" in question or "map2" in question:
                    result["map2"] = market_data

            return result

    except Exception as e:
        print(f"Ошибка get_event_data({slug}): {e}")
        return None


def liquidity_ok(data):
    """Проверяет достаточность ликвидности"""
    if not data:
        return False
    series = data.get("series")
    if not series or series["volume"] < MIN_SERIES_LIQUIDITY:
        return False
    return True


def format_preview(data, slug):
    """Форматирует превью-уведомление за 30 минут"""
    series = data.get("series")
    map1 = data.get("map1")
    title = data.get("title", slug)

    if not series:
        return None

    # Определяем фаворита
    if series["price1"] >= series["price2"]:
        fav_name = series["team1"]
        dog_name = series["team2"]
        fav_price = series["price1"]
        dog_price = series["price2"]
    else:
        fav_name = series["team2"]
        dog_name = series["team1"]
        fav_price = series["price2"]
        dog_price = series["price1"]

    band_range, band_data = get_band(fav_price)

    # Ликвидность
    series_liq = series["volume"]
    map1_liq = map1["volume"] if map1 else 0

    liq_ok = series_liq >= MIN_SERIES_LIQUIDITY and map1_liq >= MIN_MAP_LIQUIDITY
    liq_icon = "✅" if liq_ok else "⚠️"
    verdict = "Кандидат для торговли" if liq_ok and band_range and band_range[0] <= 70 else "Пропустить (мало маржи или ликвидности)"

    msg = f"""⏰ <b>МАТЧ ЧЕРЕЗ 30 МИНУТ</b>
⚔️ <b>{title}</b>

📊 <b>Серия:</b> {fav_name} {fav_price}¢ / {dog_name} {dog_price}¢
💧 Объём серии: ${series_liq:,.0f}"""

    if map1:
        msg += f"""
🗺 <b>К1:</b> {map1['team1']} {map1['price1']}¢ / {map1['team2']} {map1['price2']}¢
💧 Объём К1: ${map1_liq:,.0f}"""

    if band_range and band_data:
        msg += f"""

📉 <b>Диапазон фаворита:</b> {band_range[0]}-{band_range[1]}%
   Сдвиг серии если фав берёт К1: +{band_data['fav_win']}
   Сдвиг серии если аут берёт К1: {band_data['dog_win']}"""

    msg += f"""

{liq_icon} <b>Вердикт:</b> {verdict}"""

    return msg


def format_map1_result(data, slug, score, winner_is_home, home, away):
    """Форматирует уведомление после К1"""
    series = data.get("series")
    map2 = data.get("map2")
    title = data.get("title", f"{home} vs {away}")

    if not series:
        return None

    winner = home if winner_is_home else away
    loser = away if winner_is_home else home

    # Текущая серия
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

    # Прогноз при 1:1 из базы
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
    """Ждёт delay_seconds и отправляет превью"""
    await asyncio.sleep(delay_seconds)

    # Проверяем что матч ещё не начался
    state = match_states.get(slug, {})
    if state.get("period") not in [None, "", "not_started"]:
        return  # Уже начался, превью не нужно

    data = await get_event_data(slug)
    if not data:
        return

    if not liquidity_ok(data):
        return  # Мало ликвидности — не беспокоим

    msg = format_preview(data, slug)
    if msg:
        await send_telegram(msg)
        print(f"Превью отправлено: {slug}")
        match_states[slug]["notified_preview"] = True


async def handle_message(data):
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

    # Инициализируем состояние
    if slug not in match_states:
        match_states[slug] = {
            "period": None,
            "status": None,
            "notified_preview": False,
            "notified_map1": False,
        }

    prev_status = match_states[slug].get("status")
    prev_period = match_states[slug].get("period")

    match_states[slug]["period"] = period
    match_states[slug]["status"] = status

    # Матч появился как "not_started" — планируем превью за 30 минут
    if status == "not_started" and prev_status is None:
        if not match_states[slug]["notified_preview"] and slug not in preview_tasks:
            # Планируем через 0 секунд (сразу при обнаружении)
            # В реальности тут нужно смотреть на startDate из Gamma API
            # Пока планируем через 1 секунду как демо
            task = asyncio.create_task(send_preview(slug, delay_seconds=1))
            preview_tasks[slug] = task
            print(f"Запланировано превью: {slug}")

    # К1 закончилась = период переключился с 1/3 на 2/3
    if prev_period == "1/3" and period == "2/3":
        if not match_states[slug]["notified_map1"]:
            match_states[slug]["notified_map1"] = True

            # Определяем победителя К1 из score (формат: "000-000|1-0|Bo3")
            winner_is_home = True  # дефолт
            try:
                parts = score.split("|")
                if len(parts) >= 2:
                    map_score = parts[1]  # "1-0" или "0-1"
                    home_maps, away_maps = map_score.split("-")
                    winner_is_home = int(home_maps) > int(away_maps)
            except Exception:
                pass

            data = await get_event_data(slug)
            if data and liquidity_ok(data):
                msg = format_map1_result(data, slug, score, winner_is_home, home, away)
                if msg:
                    await send_telegram(msg)
                    print(f"Уведомление К1: {slug}")
            elif data:
                # Мало ликвидности — короткое уведомление
                winner = home if winner_is_home else away
                await send_telegram(
                    f"ℹ️ К1 закончилась: {home} vs {away}\n"
                    f"Победитель: {winner}\n"
                    f"⚠️ Мало ликвидности — пропускаем"
                )


async def schedule_previews():
    """
    Раз в 5 минут проверяет предстоящие CS2 матчи через Gamma API
    и планирует превью за 30 минут до старта
    """
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

                    # Пропускаем уже обработанные
                    if match_states.get(slug, {}).get("notified_preview"):
                        continue
                    if slug in preview_tasks:
                        continue

                    try:
                        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                        seconds_until = (start_dt - now).total_seconds()

                        # Если до старта 25-35 минут — отправляем сразу
                        # Если больше 35 минут — планируем через (seconds_until - 30*60) секунд
                        if 0 < seconds_until <= 35 * 60:
                            delay = max(0, seconds_until - 30 * 60)
                            if slug not in match_states:
                                match_states[slug] = {
                                    "period": None,
                                    "status": None,
                                    "notified_preview": False,
                                    "notified_map1": False,
                                }
                            task = asyncio.create_task(send_preview(slug, delay_seconds=delay))
                            preview_tasks[slug] = task
                            print(f"Запланировано превью через {delay:.0f}с: {slug}")

                    except Exception:
                        continue

        except Exception as e:
            print(f"Ошибка schedule_previews: {e}")

        await asyncio.sleep(5 * 60)  # Проверяем каждые 5 минут


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
                        await handle_message(data)
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            print(f"WebSocket ошибка: {e}, переподключение через 5с...")
            await asyncio.sleep(5)


async def main():
    print("Бот запущен...")
    await send_telegram("✅ <b>Polymarket CS2 бот запущен!</b>\n\nБуду присылать:\n• Превью за 30 мин до матча\n• Уведомление сразу после К1")

    # Запускаем параллельно WebSocket и планировщик превью
    await asyncio.gather(
        websocket_listener(),
        schedule_previews(),
    )


if __name__ == "__main__":
    asyncio.run(main())
