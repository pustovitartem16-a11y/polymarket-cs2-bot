import asyncio
import json
import re
import httpx
import os
from datetime import datetime, timezone, date

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_URL = "https://polymarket.com/sports/esports"

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

# Храним состояния матчей
match_states = {}
# Уже отправленные превью
notified_slugs = set()


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


async def get_cs2_slugs_from_page():
    """Парсит страницу Polymarket и возвращает уникальные CS2 slugs сегодняшнего дня"""
    try:
        today = date.today().strftime("%Y-%m-%d")
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                POLYMARKET_URL,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"}
            )
            # Ищем все cs2 slugs
            all_slugs = re.findall(r'cs2-[a-z0-9-]+', r.text)
            # Фильтруем уникальные и только основные матчи (без game1/game2/handicap/total)
            seen = set()
            result = []
            for slug in all_slugs:
                # Берём только основные slugs матчей (формат: cs2-team1-team2-date)
                # Исключаем подрынки (game1, game2, round, handicap, total)
                parts = slug.split("-")
                # Основной slug содержит дату в конце (2026-06-17)
                if len(parts) >= 5 and parts[-3].isdigit() and parts[-2].isdigit() and parts[-1].isdigit():
                    # Это основной slug матча
                    match_date = f"{parts[-3]}-{parts[-2]}-{parts[-1]}"
                    if match_date == today and slug not in seen:
                        seen.add(slug)
                        result.append(slug)
            return result
    except Exception as e:
        print(f"Ошибка парсинга страницы: {e}")
        return []


async def get_event_by_slug(slug):
    """Достаёт данные матча по slug"""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{GAMMA_API}/events", params={"slug": slug, "limit": 1})
            data = r.json()
            if data and isinstance(data, list):
                return data[0]
    except Exception as e:
        print(f"Ошибка get_event_by_slug({slug}): {e}")
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


async def send_preview(slug, data):
    """Отправляет превью матча"""
    series = data.get("series")
    map1 = data.get("map1")
    title = data.get("title", slug)
    start = data.get("start_date", "")[:16].replace("T", " ")

    if not series:
        return

    if series["volume"] < MIN_SERIES_LIQUIDITY:
        await send_telegram(
            f"📋 <b>НОВЫЙ МАТЧ</b>\n"
            f"⚔️ <b>{title}</b>\n"
            f"🕐 Начало: {start}\n"
            f"⚠️ Мало ликвидности (${series['volume']:,.0f}) — пропускаем"
        )
        return

    fav_price = max(series["price1"], series["price2"])
    band_range, band_data = get_band(fav_price)
    map1_liq = map1["volume"] if map1 else 0
    liq_ok = map1_liq >= 3000 and band_range and band_range[0] <= 70
    liq_icon = "✅" if liq_ok else "⚠️"
    verdict = "Кандидат для торговли" if liq_ok else "Пропустить"

    msg = f"""📋 <b>CS2 МАТЧ ОБНАРУЖЕН</b>
⚔️ <b>{title}</b>
🕐 Начало: {start}

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
    print(f"[ПРЕВЬЮ] {title}")


async def scan_matches():
    """Каждые 2 минуты сканирует страницу и отправляет превью новых матчей"""
    while True:
        try:
            slugs = await get_cs2_slugs_from_page()
            print(f"Найдено CS2 slugs: {slugs}")

            for slug in slugs:
                if slug in notified_slugs:
                    continue

                event = await get_event_by_slug(slug)
                if not event:
                    # Попробуем через search
                    continue

                data = parse_markets(event)
                if data.get("series"):
                    notified_slugs.add(slug)
                    await send_preview(slug, data)

                    # Сохраняем в match_states для отслеживания К1/К2
                    if slug not in match_states:
                        match_states[slug] = {
                            "title": data["title"],
                            "map1_done": False,
                            "map2_done": False,
                            "final_done": False,
                            "map1_winner": None,
                        }

        except Exception as e:
            print(f"Ошибка scan_matches: {e}")

        await asyncio.sleep(120)  # Каждые 2 минуты


async def check_active_matches():
    """Каждые 3 минуты проверяет активные матчи на смену карт"""
    while True:
        await asyncio.sleep(180)
        try:
            for slug, state in list(match_states.items()):
                if state.get("final_done"):
                    continue

                event = await get_event_by_slug(slug)
                if not event:
                    continue

                data = parse_markets(event)
                series = data.get("series")
                map2 = data.get("map2")
                map3 = data.get("map3")
                title = state.get("title", slug)

                if not series:
                    continue

                # Определяем текущее состояние по рынкам
                # Если К2 рынок открыт и К1 уже закрыта
                map1 = data.get("map1")

                # Проверяем закрылась ли К1 (map1 market closed или цена 0/100)
                if map1 and not state["map1_done"]:
                    p1 = map1["price1"]
                    p2 = map1["price2"]
                    if p1 >= 95 or p2 >= 95:  # Карта почти завершена
                        state["map1_done"] = True
                        winner = map1["team1"] if p1 >= 95 else map1["team2"]
                        state["map1_winner"] = winner

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
🏆 К1 взял: <b>{winner}</b>

📈 <b>Серия сейчас:</b>
  {cur_fav} {cur_fav_p}¢ / {cur_dog} {cur_dog_p}¢
  Объём: ${series['volume']:,.0f}"""

                        if map2:
                            msg += f"""
🗺 <b>К2:</b>
  {map2['team1']} {map2['price1']}¢ / {map2['team2']} {map2['price2']}¢"""

                        if band_data:
                            msg += f"""

🤖 <b>Прогноз (база {band_range[0]}-{band_range[1]}%):</b>
  При 1:1 серия {cur_fav} → ~{revert}¢"""

                        msg += "\n\n⚡️ <b>У тебя ~5 минут — открывай калькулятор!</b>"
                        await send_telegram(msg)
                        print(f"[К1] {title}: {winner}")

                # Проверяем завершение серии
                s1 = series["price1"]
                s2 = series["price2"]
                if (s1 >= 98 or s2 >= 98) and not state["final_done"]:
                    state["final_done"] = True
                    winner = series["team1"] if s1 >= 98 else series["team2"]
                    await send_telegram(
                        f"🏆 <b>СЕРИЯ ЗАВЕРШЕНА!</b>\n"
                        f"⚔️ <b>{title}</b>\n"
                        f"🥇 Победитель: <b>{winner}</b>\n\n"
                        f"✅ Серия нога резолвится автоматически"
                    )
                    print(f"[ФИНАЛ] {title}: {winner}")

        except Exception as e:
            print(f"Ошибка check_active_matches: {e}")


async def main():
    print("Бот запущен...")
    await send_telegram(
        "✅ <b>Polymarket CS2 бот запущен!</b>\n\n"
        "Буду присылать:\n"
        "• Превью новых матчей (сканирование каждые 2 мин)\n"
        "• После К1 — цены и прогноз\n"
        "• После завершения серии — итог"
    )

    # Запускаем сканирование сразу
    await asyncio.gather(
        scan_matches(),
        check_active_matches(),
    )


if __name__ == "__main__":
    asyncio.run(main())
