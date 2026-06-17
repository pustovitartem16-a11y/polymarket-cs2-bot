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

match_states = {}
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
    """Парсит страницу и возвращает основные CS2 slugs сегодня"""
    try:
        today = date.today().strftime("%Y-%m-%d")
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(POLYMARKET_URL, headers={"User-Agent": "Mozilla/5.0"})
            all_slugs = re.findall(r'cs2-[a-z0-9-]+', r.text)
            seen = set()
            result = []
            for slug in all_slugs:
                parts = slug.split("-")
                # Основной slug: cs2-team1-team2-YYYY-MM-DD (заканчивается датой)
                if len(parts) >= 5:
                    tail = "-".join(parts[-3:])
                    if re.match(r'^\d{4}-\d{2}-\d{2}$', tail):
                        if tail == today and slug not in seen:
                            seen.add(slug)
                            result.append(slug)
            return result
    except Exception as e:
        print(f"Ошибка парсинга: {e}")
        return []


async def get_event_by_slug(slug):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{GAMMA_API}/events", params={"slug": slug, "limit": 1})
            data = r.json()
            if data and isinstance(data, list):
                return data[0]
    except Exception as e:
        print(f"Ошибка get_event({slug}): {e}")
    return None


def parse_markets(event):
    """Парсит рынки и определяет состояние каждой карты"""
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
        closed = m.get("closed", False)
        resolved = m.get("resolved", False)

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
            "closed": closed or resolved,
            # Победитель если карта закрыта
            "winner": outcomes_list[0] if prices_float[0] >= 0.95 else (outcomes_list[1] if prices_float[1] >= 0.95 else None),
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
    """
    Определяет текущую стадию матча:
    - 'before' — до начала
    - 'map1_live' — К1 идёт
    - 'map1_done' — К1 завершена, К2 ещё не началась / идёт
    - 'map2_live' — К2 идёт
    - 'map2_done_sweep' — 2:0, серия завершена
    - 'map2_done_split' — 1:1, К3 впереди
    - 'map3_live' — К3 идёт
    - 'finished' — серия завершена
    """
    series = data.get("series")
    map1 = data.get("map1")
    map2 = data.get("map2")
    map3 = data.get("map3")

    if not series:
        return "unknown"

    s1, s2 = series["price1"], series["price2"]

    # Серия завершена
    if s1 >= 98 or s2 >= 98:
        return "finished"

    # К3 идёт или завершена
    if map3:
        if map3["price1"] >= 95 or map3["price2"] >= 95:
            return "finished"
        return "map3_live"

    # К2 ситуация
    if map2:
        p1, p2 = map2["price1"], map2["price2"]
        if p1 >= 95 or p2 >= 95:
            # К2 завершена — определяем sweep или split
            if map1 and map1.get("winner"):
                m1_winner = map1["winner"]
                m2_winner = map2["team1"] if p1 >= 95 else map2["team2"]
                if m1_winner == m2_winner:
                    return "map2_done_sweep"
                else:
                    return "map2_done_split"
            # Смотрим по серии — если серия почти решена
            if s1 >= 90 or s2 >= 90:
                return "map2_done_sweep"
            return "map2_done_split"
        # К2 идёт (цены между 5-95)
        if 5 < p1 < 95 and 5 < p2 < 95:
            return "map2_live"
        return "map1_done"

    # К1 ситуация
    if map1:
        p1, p2 = map1["price1"], map1["price2"]
        if p1 >= 95 or p2 >= 95:
            return "map1_done"
        if 5 < p1 < 95 and 5 < p2 < 95:
            return "map1_live"

    return "before"


async def send_preview(slug, data):
    """Превью матча за ~30 минут"""
    series = data.get("series")
    map1 = data.get("map1")
    title = data.get("title", slug)
    start = data.get("start_date", "")[:16].replace("T", " ")

    if not series:
        return

    if series["volume"] < MIN_SERIES_LIQUIDITY:
        await send_telegram(
            f"📋 <b>НОВЫЙ CS2 МАТЧ</b>\n"
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


async def send_map1_done(slug, data):
    """Уведомление после К1"""
    series = data.get("series")
    map1 = data.get("map1")
    map2 = data.get("map2")
    title = data.get("title", slug)

    if not series or series["volume"] < MIN_SERIES_LIQUIDITY:
        return

    winner = map1.get("winner") if map1 else None
    if not winner:
        # Определяем по ценам
        if map1:
            winner = map1["team1"] if map1["price1"] >= 95 else map1["team2"]

    fav_price = max(series["price1"], series["price2"])
    if series["price1"] >= series["price2"]:
        cur_fav, cur_fav_p = series["team1"], series["price1"]
        cur_dog, cur_dog_p = series["team2"], series["price2"]
    else:
        cur_fav, cur_fav_p = series["team2"], series["price2"]
        cur_dog, cur_dog_p = series["team1"], series["price1"]

    band_range, band_data = get_band(cur_fav_p)
    revert = band_data["revert"] if band_data else "~50"

    # Прогноз только если логичный (фаворит в нормальном диапазоне 50-80)
    show_forecast = band_range and band_range[0] >= 50 and band_range[1] <= 80

    msg = f"""🎮 <b>К1 ЗАКОНЧИЛАСЬ!</b>
⚔️ <b>{title}</b>
🏆 К1 взял: <b>{winner or "неизвестно"}</b>

📈 <b>Серия сейчас:</b>
  {cur_fav} {cur_fav_p}¢ / {cur_dog} {cur_dog_p}¢
  Объём: ${series['volume']:,.0f}"""

    if map2 and 5 < map2["price1"] < 95:
        msg += f"""

🗺 <b>К2:</b>
  {map2['team1']} {map2['price1']}¢ / {map2['team2']} {map2['price2']}¢
  Объём: ${map2['volume']:,.0f}"""

    if show_forecast and band_data:
        msg += f"""

🤖 <b>Прогноз (база {band_range[0]}-{band_range[1]}%):</b>
  При 1:1 серия {cur_fav} → ~{revert}¢"""
    elif not show_forecast:
        msg += f"""

⚠️ <b>Прогноз не применим:</b> фаворит {cur_fav_p}¢ — слишком высокий для стратегии"""

    msg += "\n\n⚡️ <b>У тебя ~5 минут — открывай калькулятор!</b>"
    await send_telegram(msg)
    print(f"[К1 DONE] {title}: {winner}")


async def send_map2_done_sweep(slug, data):
    """2:0 sweep"""
    series = data.get("series")
    title = data.get("title", slug)
    if not series:
        return
    winner = series["team1"] if series["price1"] >= 95 else series["team2"]
    await send_telegram(
        f"🏆 <b>СЕРИЯ ЗАКОНЧИЛАСЬ! 2:0 SWEEP</b>\n"
        f"⚔️ <b>{title}</b>\n"
        f"🥇 Победитель: <b>{winner}</b>\n\n"
        f"✅ Обе ноги резолвятся автоматически\n"
        f"💰 Профит зачислится на счёт Polymarket"
    )
    print(f"[SWEEP] {title}: {winner}")


async def send_map2_done_split(slug, data, map1_winner):
    """1:1 — К3 впереди"""
    series = data.get("series")
    map2 = data.get("map2")
    map3 = data.get("map3")
    title = data.get("title", slug)
    if not series:
        return

    # Победитель К2 — противоположный К1
    if map2:
        map2_winner = map2["team1"] if map2["price1"] >= 95 else map2["team2"]
    else:
        map2_winner = "неизвестно"

    msg = f"""⚖️ <b>К2 ЗАКОНЧИЛАСЬ! СЧЁТ 1:1</b>
⚔️ <b>{title}</b>
🗺 К2 взял: <b>{map2_winner}</b>

📈 <b>Серия сейчас:</b>
  {series['team1']} {series['price1']}¢ / {series['team2']} {series['price2']}¢"""

    if map3 and map3["volume"] > 0:
        msg += f"""
🗺 <b>К3:</b>
  {map3['team1']} {map3['price1']}¢ / {map3['team2']} {map3['price2']}¢"""

    msg += f"""

✅ <b>Что делать:</b>
Нога «К2» резолвится в 100¢ → профит зафиксирован
<b>Продай ногу «СЕРИЯ»</b> по текущей цене прямо сейчас!
Не жди К3 — фиксируй гарантированный профит

⚡️ Открывай Polymarket и продавай серию ногу!"""

    await send_telegram(msg)
    print(f"[SPLIT] {title}: 1:1")


async def send_finished(slug, data):
    """Серия завершена"""
    series = data.get("series")
    title = data.get("title", slug)
    if not series:
        return
    winner = series["team1"] if series["price1"] >= 95 else series["team2"]
    await send_telegram(
        f"🏁 <b>СЕРИЯ ЗАВЕРШЕНА</b>\n"
        f"⚔️ <b>{title}</b>\n"
        f"🥇 Победитель: <b>{winner}</b>\n\n"
        f"✅ Серия нога резолвится автоматически"
    )
    print(f"[ФИНАЛ] {title}: {winner}")


async def scan_matches():
    """Каждые 2 минуты сканирует страницу на новые матчи"""
    while True:
        try:
            slugs = await get_cs2_slugs_from_page()
            print(f"[SCAN] Найдено slugs: {len(slugs)} → {slugs}")

            for slug in slugs:
                if slug in notified_slugs:
                    continue

                event = await get_event_by_slug(slug)
                if not event:
                    continue

                data = parse_markets(event)
                if not data.get("series"):
                    continue

                notified_slugs.add(slug)
                stage = get_match_stage(data)

                # Инициализируем состояние
                match_states[slug] = {
                    "title": data["title"],
                    "stage": stage,
                    "map1_winner": None,
                    "notified_map1": False,
                    "notified_map2": False,
                    "notified_final": False,
                }

                # Отправляем превью только если матч ещё не начался или только начался
                if stage in ["before", "map1_live"]:
                    await send_preview(slug, data)
                elif stage == "map1_done":
                    # Матч уже идёт — только информируем
                    await send_telegram(
                        f"ℹ️ <b>Матч уже идёт (К2)</b>\n"
                        f"⚔️ <b>{data['title']}</b>\n"
                        f"📊 Серия: {data['series']['team1']} {data['series']['price1']}¢ / "
                        f"{data['series']['team2']} {data['series']['price2']}¢"
                    )
                # Остальные стадии — игнорируем (матч уже прошёл)

        except Exception as e:
            print(f"Ошибка scan_matches: {e}")

        await asyncio.sleep(120)


async def check_active_matches():
    """Каждые 90 секунд проверяет активные матчи"""
    await asyncio.sleep(30)  # Даём время scan_matches сначала

    while True:
        try:
            for slug, state in list(match_states.items()):
                if state.get("notified_final"):
                    continue

                event = await get_event_by_slug(slug)
                if not event:
                    continue

                data = parse_markets(event)
                stage = get_match_stage(data)
                prev_stage = state.get("stage", "before")
                state["stage"] = stage

                title = state.get("title", slug)
                series = data.get("series")
                map1 = data.get("map1")

                if not series or series["volume"] < MIN_SERIES_LIQUIDITY:
                    continue

                # К1 завершилась
                if stage in ["map1_done", "map2_live", "map2_done_sweep", "map2_done_split", "map3_live", "finished"] \
                        and prev_stage in ["before", "map1_live"] \
                        and not state["notified_map1"]:
                    state["notified_map1"] = True
                    if map1:
                        state["map1_winner"] = map1.get("winner") or (map1["team1"] if map1["price1"] >= 95 else map1["team2"])
                    await send_map1_done(slug, data)

                # К2 завершилась — sweep
                if stage == "map2_done_sweep" and not state["notified_map2"]:
                    state["notified_map2"] = True
                    state["notified_final"] = True
                    await send_map2_done_sweep(slug, data)

                # К2 завершилась — split 1:1
                elif stage == "map2_done_split" and not state["notified_map2"]:
                    state["notified_map2"] = True
                    await send_map2_done_split(slug, data, state.get("map1_winner"))

                # Серия завершена (после К3)
                elif stage == "finished" and not state["notified_final"]:
                    state["notified_final"] = True
                    await send_finished(slug, data)

        except Exception as e:
            print(f"Ошибка check_active_matches: {e}")

        await asyncio.sleep(90)


async def main():
    print("Бот запущен...")
    await send_telegram(
        "✅ <b>Polymarket CS2 бот запущен!</b>\n\n"
        "Буду присылать:\n"
        "• 📋 Превью новых матчей\n"
        "• 🎮 После К1 — цены, К2 цены и прогноз\n"
        "• ⚖️ После К2 — что делать с позицией\n"
        "• 🏆 Итог серии"
    )

    await asyncio.gather(
        scan_matches(),
        check_active_matches(),
    )


if __name__ == "__main__":
    asyncio.run(main())
