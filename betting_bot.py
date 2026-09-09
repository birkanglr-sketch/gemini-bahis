"""
Sanal Bahis Kuponu Botu — 1 Haftalık Deneme/Yarışma
====================================================================
NE YAPAR:
- The Odds API (ücretsiz katman) üzerinden futbol (büyük Avrupa ligleri
  + Şampiyonlar Ligi), NBA ve mevcut tenis turnuvalarının GÜNCEL
  bahis oranlarını çeker.
- Dengeli-agresif bir mantıkla seçim yapar: oranı çok düşük (aşırı
  güvenli/sıkıcı) veya çok yüksek (anlamsız sürpriz) olan maçları
  eler, kalan en iyi 1-3 seçimi bir araya getirip "kupon" oluşturur.
  Yeterince iyi seçim yoksa TEK MAÇA da oynar.
- SANAL bakiye üzerinden çalışır, gerçek para hareket ETMEZ.
- Maçlar bitince sonuçları çekip kuponu otomatik KAZANDI/KAYBETTİ
  olarak kapatır, bakiyeyi günceller.
- Her şeyi bets_log.csv dosyasına, güncel durumu state.json'a yazar.

GEREKLİ: Ücretsiz bir API anahtarı — https://the-odds-api.com adresinden
"Get API Key" ile kayıt olun (kredi kartı istemez, ayda 500 istek
ücretsiz). Aldığınız anahtarı GitHub repo Secrets'a ODDS_API_KEY olarak
ekleyeceksiniz (kurulum talimatlarında anlatılıyor).
"""

import requests
import json
import csv
import os
from datetime import datetime, timezone, timedelta

# =====================================================================
# AYARLAR
# =====================================================================
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
REGION = "eu"
MARKET = "h2h"   # maç sonucu (1X2 / kazanan)

# Taranacak spor/lig anahtarları (The Odds API sport keys)
SPORT_KEYS = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
    "basketball_nba",
    "tennis_atp_us_open",
    "tennis_wta_us_open",
]

STARTING_BANKROLL = 2000.0
STAKE_PCT_OF_BANKROLL = 0.08     # her kupona bakiyenin %8'i kadar bahis
MAX_OPEN_COUPONS = 3             # aynı anda en fazla kaç kupon bekliyor olsun
MIN_LEGS = 1                     # yeterli seçim yoksa tek maça da oynar
MAX_LEGS = 3
MIN_ODDS_PER_AYAK = 1.30         # bu oranın altı: çok sıkıcı/düşük getiri, atlanır
MAX_ODDS_PER_AYAK = 3.00         # bu oranın üstü: çok riskli sürpriz, atlanır
MAX_HOURS_AHEAD = 30             # sadece bu kadar saat içinde başlayacak maçlara bakılır (her gün oynasın diye)
SETTLE_BUFFER_HOURS = 3          # maç bitiminden bu kadar saat sonra sonucu kesin sayar

STATE_FILE = "state.json"
LOG_FILE = "bets_log.csv"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# =====================================================================


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print(f"[Telegram hata]: {e}")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"bankroll": STARTING_BANKROLL, "pending_coupons": [], "starting_bankroll": STARTING_BANKROLL}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def log_row(action, detail, bankroll_after):
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp", "action", "detail", "bankroll_after"])
        writer.writerow([datetime.now(timezone.utc).isoformat(), action, detail, f"{bankroll_after:.2f}"])


def fetch_odds(sport_key):
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
    params = {"apiKey": ODDS_API_KEY, "regions": REGION, "markets": MARKET, "oddsFormat": "decimal"}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            print(f"[Uyarı] {sport_key} oranları alınamadı ({r.status_code})")
            return []
        return r.json()
    except Exception as e:
        print(f"[Uyarı] {sport_key} isteğinde hata: {e}")
        return []


def fetch_scores(sport_key, days_from=3):
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores"
    params = {"apiKey": ODDS_API_KEY, "daysFrom": days_from}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return []
        return r.json()
    except Exception:
        return []


def collect_candidate_legs(already_used_event_ids):
    """Tüm sporları tarar, oynanabilir aday seçimleri (ayaklar) döndürür."""
    candidates = []
    for sport_key in SPORT_KEYS:
        events = fetch_odds(sport_key)
        for event in events:
            if event.get("id") in already_used_event_ids:
                continue
            if not event.get("bookmakers"):
                continue
            market = next((m for b in event["bookmakers"] for m in b.get("markets", []) if m["key"] == MARKET), None)
            if not market or not market.get("outcomes"):
                continue

            # En düşük oranlı (favori) seçeneği al
            outcomes = sorted(market["outcomes"], key=lambda o: o["price"])
            favorite = outcomes[0]
            odds = favorite["price"]

            if not (MIN_ODDS_PER_AYAK <= odds <= MAX_ODDS_PER_AYAK):
                continue

            commence_time = event.get("commence_time")
            if not commence_time:
                continue
            commence = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
            hours_ahead = (commence - datetime.now(timezone.utc)).total_seconds() / 3600
            if hours_ahead < 0 or hours_ahead > MAX_HOURS_AHEAD:
                continue

            candidates.append({
                "sport_key": sport_key,
                "event_id": event["id"],
                "home_team": event.get("home_team", "?"),
                "away_team": event.get("away_team", "?"),
                "commence_time": commence_time,
                "pick": favorite["name"],
                "odds": odds,
            })

    # En düşük (en güvenilir) orandan başlayarak sırala — dengeli/agresif karışım
    candidates.sort(key=lambda c: c["odds"])
    return candidates


def place_coupons(state):
    used_ids = {leg["event_id"] for c in state["pending_coupons"] for leg in c["legs"]}
    open_slots = MAX_OPEN_COUPONS - len(state["pending_coupons"])
    if open_slots <= 0:
        print("Açık kupon limiti dolu, yeni kupon aranmadı.")
        return

    candidates = collect_candidate_legs(used_ids)
    print(f"📡 {len(candidates)} aday seçim bulundu ({len(SPORT_KEYS)} lig/spor tarandı).")

    idx = 0
    for _ in range(open_slots):
        if len(state["pending_coupons"]) >= MAX_OPEN_COUPONS:
            print("Güvenlik kontrolü: kupon limiti doldu, döngü durduruldu.")
            break
        if idx >= len(candidates):
            break
        # Elde kalan adaylardan en fazla MAX_LEGS kadarını al, en az MIN_LEGS gerekli
        remaining = candidates[idx:]
        if not remaining:
            break
        legs = remaining[:MAX_LEGS] if len(remaining) >= MAX_LEGS else remaining[:max(MIN_LEGS, len(remaining))]
        if len(legs) < MIN_LEGS:
            break
        idx += len(legs)

        stake = state["bankroll"] * STAKE_PCT_OF_BANKROLL
        if stake < 5:
            print("Bakiye çok düşük, yeni kupon açılmadı.")
            break

        combined_odds = 1.0
        for leg in legs:
            combined_odds *= leg["odds"]
        potential_return = stake * combined_odds

        coupon = {
            "legs": legs,
            "stake": stake,
            "combined_odds": round(combined_odds, 3),
            "potential_return": round(potential_return, 2),
            "placed_at": datetime.now(timezone.utc).isoformat(),
        }
        state["bankroll"] -= stake
        state["pending_coupons"].append(coupon)

        leg_desc = " + ".join(f"{l['pick']} ({l['home_team']} vs {l['away_team']} @ {l['odds']})" for l in legs)
        detail = f"{len(legs)} ayaklı kupon: {leg_desc} | Toplam oran: {combined_odds:.2f} | Bahis: {stake:.2f} USD"
        log_row("PLACE", detail, state["bankroll"])
        msg = f"🎟️ Yeni kupon — {detail}\nMuhtemel kazanç: {potential_return:.2f} USD"
        print(msg)
        send_telegram(msg)


def settle_coupons(state):
    still_pending = []
    scores_cache = {}

    for coupon in state["pending_coupons"]:
        all_finished = True
        all_won = True
        results_desc = []

        for leg in coupon["legs"]:
            sport_key = leg["sport_key"]
            if sport_key not in scores_cache:
                scores_cache[sport_key] = fetch_scores(sport_key)
            match = next((s for s in scores_cache[sport_key] if s.get("id") == leg["event_id"]), None)

            commence = datetime.fromisoformat(leg["commence_time"].replace("Z", "+00:00"))
            enough_time_passed = datetime.now(timezone.utc) > commence + timedelta(hours=SETTLE_BUFFER_HOURS)

            if not match or not match.get("completed"):
                if enough_time_passed:
                    # Maç bitmiş olmalı ama veri gelmemiş — bir sonraki çalıştırmada tekrar denenir
                    all_finished = False
                else:
                    all_finished = False
                continue

            scores = {s["name"]: float(s["score"]) for s in match.get("scores", []) if s.get("score") is not None}
            if leg["home_team"] not in scores or leg["away_team"] not in scores:
                all_finished = False
                continue

            home_score = scores[leg["home_team"]]
            away_score = scores[leg["away_team"]]
            if home_score > away_score:
                winner = leg["home_team"]
            elif away_score > home_score:
                winner = leg["away_team"]
            else:
                winner = "Draw"

            leg_won = (winner == leg["pick"])
            results_desc.append(f"{leg['home_team']} vs {leg['away_team']}: {winner} kazandı, seçim {leg['pick']} → {'✓' if leg_won else '✗'}")
            if not leg_won:
                all_won = False

        if not all_finished:
            still_pending.append(coupon)
            continue

        if all_won:
            state["bankroll"] += coupon["potential_return"]
            pnl = coupon["potential_return"] - coupon["stake"]
            detail = f"KAZANDI | {' | '.join(results_desc)} | PnL: +{pnl:.2f} USD"
            msg = f"✅ Kupon KAZANDI — Kazanç: {coupon['potential_return']:.2f} USD (PnL: +{pnl:.2f})"
        else:
            pnl = -coupon["stake"]
            detail = f"KAYBETTİ | {' | '.join(results_desc)} | PnL: {pnl:.2f} USD"
            msg = f"❌ Kupon KAYBETTİ — Kayıp: {coupon['stake']:.2f} USD"

        log_row("SETTLE", detail, state["bankroll"])
        print(msg)
        send_telegram(msg)

    state["pending_coupons"] = still_pending


def main():
    if not ODDS_API_KEY:
        print("HATA: ODDS_API_KEY tanımlı değil. GitHub Secrets'a eklemeyi unutmayın.")
        return

    state = load_state()

    settle_coupons(state)
    place_coupons(state)

    open_stake = sum(c["stake"] for c in state["pending_coupons"])
    total_value = state["bankroll"] + open_stake
    total_pnl_pct = ((total_value - state["starting_bankroll"]) / state["starting_bankroll"]) * 100
    print(f"\n📊 Nakit: {state['bankroll']:.2f} USD | Bekleyen kupon sayısı: {len(state['pending_coupons'])} | "
          f"Toplam değer: {total_value:.2f} USD | Toplam PnL: {total_pnl_pct:.2f}%")

    save_state(state)


if __name__ == "__main__":
    main()
