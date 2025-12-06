# scanner.py - High Probability Token Scanner
# 
# Scans Polymarket for high probability tokens (price >= 0.90)
# Prioritizes tokens that end soonest
# Outputs to opportunities.json for the trading bot
#
# Run periodically via cron: */60 * * * * python3 /path/to/scanner.py
#
# IMPORTANT: This version includes token_id (clobTokenIds) needed for trading!

import json
from typing import Any, Dict, List, Optional

import requests
from datetime import datetime, timezone
from dateutil import parser as date_parser  # pip install python-dateutil
import pytz  # pip install pytz


def load_config(path: str = "config.json") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_price_range(config: Dict[str, Any]) -> None:
    """
    Normalizuje zakresy cen z ewentualnych procentów (30–70) na 0–1 (0.3–0.7).
    """
    def _normalize_pair(min_key: str, max_key: str) -> None:
        if min_key not in config or max_key not in config:
            return
        pmin = config[min_key]
        pmax = config[max_key]
        if pmax is None or pmin is None:
            return
        if pmax > 1.5:  # wygląda na procenty
            config[min_key] = pmin / 100.0
            config[max_key] = pmax / 100.0
            print(
                f"[WARN] {min_key}/{max_key} wyglądały na procenty – "
                f"znormalizowano do {config[min_key]} – {config[max_key]}"
            )

    _normalize_pair("price_min", "price_max")
    _normalize_pair("price_min_yes", "price_max_yes")
    _normalize_pair("price_min_no", "price_max_no")


def fetch_markets(gamma_url: str) -> List[Dict[str, Any]]:
    """
    Pobiera WSZYSTKIE rynki z Gamma Markets API, paginując po limit+offset.
    """
    all_markets: List[Dict[str, Any]] = []

    limit = 500
    offset = 0

    while True:
        params = {
            "limit": limit,
            "offset": offset,
            "closed": "false"
        }

        print(f"[INFO] Pobieram rynki z {gamma_url} offset={offset} limit={limit}")
        try:
            resp = requests.get(gamma_url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[ERROR] Błąd pobierania rynków: {e}")
            break

        if isinstance(data, dict):
            if "markets" in data:
                page_markets = data["markets"]
            elif "data" in data:
                page_markets = data["data"]
            else:
                page_markets = []
        else:
            page_markets = data

        count_page = len(page_markets)
        print(f"[INFO] Otrzymano {count_page} rynków dla offset={offset}")

        if count_page == 0:
            break

        all_markets.extend(page_markets)

        if count_page < limit:
            break

        offset += limit

    print(f"[INFO] Łącznie zebrano {len(all_markets)} rynków z API")
    
    return all_markets


def parse_end_datetime(market: Dict[str, Any], tz_name: str) -> datetime | None:
    tz = pytz.timezone(tz_name)
    end_str = market.get("endDateIso") or market.get("endDate")
    if not end_str:
        return None
    try:
        dt = date_parser.isoparse(end_str)
    except (ValueError, TypeError):
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def extract_numeric(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_json_field(value: Any) -> List:
    """Parsuje pole które może być stringiem JSON lub listą"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return []


def get_outcome_price_range(config: Dict[str, Any], outcome: Any) -> tuple[float, float]:
    """
    Zwraca zakres cen (min, max) dla danego outcome.
    """
    outcome_str = str(outcome).lower()

    pmin = config.get("price_min", 0.0)
    pmax = config.get("price_max", 1.0)

    if "yes" in outcome_str and "price_min_yes" in config and "price_max_yes" in config:
        pmin = config["price_min_yes"]
        pmax = config["price_max_yes"]
    elif "no" in outcome_str and "price_min_no" in config and "price_max_no" in config:
        pmin = config["price_min_no"]
        pmax = config["price_max_no"]

    return pmin, pmax


def build_token_list(
    markets: List[Dict[str, Any]], config: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    Buduje listę tokenów z rynków.
    WAŻNE: Teraz zawiera token_id potrzebny do handlu!
    """
    now = datetime.now(pytz.timezone(config.get("timezone", "UTC")))

    print(
        f"[INFO] Globalny zakres cen tokenów: "
        f"{config.get('price_min')} – {config.get('price_max')}"
    )

    tokens: List[Dict[str, Any]] = []

    active_markets = 0
    markets_with_outcomes = 0
    markets_with_tokens = 0
    candidates_before_price_filter = 0

    for m in markets:
        # Filtruj zamknięte/archiwalne
        active = m.get("active")
        closed = m.get("closed")
        archived = m.get("archived")

        if active is False:
            continue
        if closed is True:
            continue
        if archived is True:
            continue

        end_dt = parse_end_datetime(m, config.get("timezone", "UTC"))
        if not end_dt:
            continue

        seconds_to_end = (end_dt - now).total_seconds()
        if seconds_to_end <= 0:
            continue

        hours_to_end = seconds_to_end / 3600.0

        active_markets += 1

        # Parsuj outcomes, prices i token_ids
        outcomes = parse_json_field(m.get("outcomes"))
        outcome_prices = parse_json_field(m.get("outcomePrices"))
        clob_token_ids = parse_json_field(m.get("clobTokenIds"))

        if not outcomes or not outcome_prices:
            continue

        markets_with_outcomes += 1

        # Sprawdź czy mamy token_ids
        has_tokens = len(clob_token_ids) >= len(outcomes)
        if has_tokens:
            markets_with_tokens += 1

        # Wolumen i płynność
        volume24hr = extract_numeric(m.get("volume24hr"))
        liquidity = extract_numeric(m.get("liquidityNum", m.get("liquidity")))
        open_interest = extract_numeric(m.get("openInterestNum", m.get("openInterest")))

        # Dane z eventu (jeśli dostępne)
        events = m.get("events") or []
        event = events[0] if events else None
        if event:
            volume24hr = max(volume24hr, extract_numeric(event.get("volume24hr")))
            liquidity = max(liquidity, extract_numeric(event.get("liquidity")))
            open_interest = max(
                open_interest,
                extract_numeric(event.get("openInterestNum", event.get("openInterest")))
            )

        # condition_id potrzebny do sprawdzania resolution
        condition_id = m.get("conditionId", "")

        for i, outcome in enumerate(outcomes):
            if i >= len(outcome_prices):
                continue
            
            price = extract_numeric(outcome_prices[i], default=-1)
            if price < 0:
                continue

            candidates_before_price_filter += 1

            # Zakres cen
            pmin, pmax = get_outcome_price_range(config, outcome)

            if price < pmin or price >= pmax:
                continue

            # Token ID - KLUCZOWE dla handlu!
            token_id = ""
            if has_tokens and i < len(clob_token_ids):
                token_id = clob_token_ids[i]

            # Upside i risk/reward
            upside = 1.0 - price
            risk = price
            rr = (upside / risk) if risk > 0 else 0.0

            tokens.append({
                "market_id": m.get("id", ""),
                "event_id": event.get("id") if event else "",
                "condition_id": condition_id,
                "slug": m.get("slug", ""),
                "question": m.get("question", ""),
                "outcome": outcome,
                "token_id": token_id,  # WAŻNE: potrzebne do handlu!
                "price": price,
                "end_date": end_dt.isoformat(),
                "seconds_to_end": seconds_to_end,
                "hours_to_end": hours_to_end,
                "upside": upside,
                "rr": rr,
                "volume24hr": volume24hr,
                "liquidity": liquidity,
                "open_interest": open_interest,
                "url": f"https://polymarket.com/event/{m.get('slug')}" if m.get("slug") else "",
            })

    print(f"[INFO] Aktywne rynki: {active_markets}")
    print(f"[INFO] Rynki z outcomes/prices: {markets_with_outcomes}")
    print(f"[INFO] Rynki z token_ids: {markets_with_tokens}")
    print(f"[INFO] Kandydaci przed filtrem cenowym: {candidates_before_price_filter}")
    print(f"[INFO] Tokeny po filtrze cenowym: {len(tokens)}")

    # Sprawdź ile ma token_id
    with_token_id = sum(1 for t in tokens if t.get("token_id"))
    print(f"[INFO] Tokeny z token_id: {with_token_id}/{len(tokens)}")

    return tokens


def sort_and_filter_tokens(
    tokens: List[Dict[str, Any]], config: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    Filtruje i sortuje tokeny według kryteriów jakości.
    PRIORYTET: Tokeny które najszybciej się kończą (najkrótszy czas do końca).
    """
    min_volume_24h = config.get("min_volume_24h", 0.0)
    min_liquidity = config.get("min_liquidity", 0.0)
    min_open_interest = config.get("min_open_interest", 0.0)
    min_upside = config.get("min_upside", 0.0)
    min_rr_ratio = config.get("min_rr_ratio", 0.0)

    min_time_hours = config.get("min_time_to_end_hours", 0.0)
    max_time_hours = config.get("max_time_to_end_hours", 1e9)

    # Wymagaj token_id (opcjonalnie)
    require_token_id = config.get("require_token_id", True)

    print(f"[INFO] Filtry: vol>={min_volume_24h}, liq>={min_liquidity}, "
          f"time=[{min_time_hours}, {max_time_hours}]h, require_token_id={require_token_id}")

    filtered: List[Dict[str, Any]] = []
    
    drop_reasons = {
        "no_token_id": 0,
        "low_volume": 0,
        "low_liquidity": 0,
        "low_oi": 0,
        "low_upside": 0,
        "low_rr": 0,
        "time_too_short": 0,
        "time_too_long": 0,
    }

    for t in tokens:
        # Wymagaj token_id
        if require_token_id and not t.get("token_id"):
            drop_reasons["no_token_id"] += 1
            continue
        
        if t["volume24hr"] < min_volume_24h:
            drop_reasons["low_volume"] += 1
            continue
        if t["liquidity"] < min_liquidity:
            drop_reasons["low_liquidity"] += 1
            continue
        if t.get("open_interest", 0.0) < min_open_interest:
            drop_reasons["low_oi"] += 1
            continue
        if t["upside"] < min_upside:
            drop_reasons["low_upside"] += 1
            continue
        if t["rr"] < min_rr_ratio:
            drop_reasons["low_rr"] += 1
            continue

        hours = t.get("hours_to_end", t["seconds_to_end"] / 3600.0)
        if hours < min_time_hours:
            drop_reasons["time_too_short"] += 1
            continue
        if hours > max_time_hours:
            drop_reasons["time_too_long"] += 1
            continue

        filtered.append(t)

    print(f"[INFO] Odrzucone - brak token_id: {drop_reasons['no_token_id']}")
    print(f"[INFO] Odrzucone - niski wolumen: {drop_reasons['low_volume']}")
    print(f"[INFO] Odrzucone - niska płynność: {drop_reasons['low_liquidity']}")
    print(f"[INFO] Odrzucone - za krótki czas: {drop_reasons['time_too_short']}")
    print(f"[INFO] Odrzucone - za długi czas: {drop_reasons['time_too_long']}")
    print(f"[INFO] Po filtrach jakości: {len(filtered)}")

    if not filtered:
        return []

    # SCORING - priorytet: czas do końca (najkrótszy = najwyższy score)
    weights = config.get("score_weights", {})
    w_time = float(weights.get("time_to_end", 0.6))  # Zwiększony priorytet czasu
    w_vol = float(weights.get("volume_24h", 0.25))
    w_liq = float(weights.get("liquidity", 0.15))

    print(f"[INFO] Wagi: time={w_time}, vol={w_vol}, liq={w_liq}")

    # Normalizacja
    min_hours = min(t["hours_to_end"] for t in filtered)
    max_hours = max(t["hours_to_end"] for t in filtered)
    min_vol = min(t["volume24hr"] for t in filtered)
    max_vol = max(t["volume24hr"] for t in filtered)
    min_liq = min(t["liquidity"] for t in filtered)
    max_liq = max(t["liquidity"] for t in filtered)

    def norm_desc(value: float, vmin: float, vmax: float) -> float:
        """Mniejsza wartość -> wyższy score (dla czasu)"""
        if vmax <= vmin:
            return 1.0
        return (vmax - value) / (vmax - vmin)

    def norm_asc(value: float, vmin: float, vmax: float) -> float:
        """Większa wartość -> wyższy score (dla vol/liq)"""
        if vmax <= vmin:
            return 1.0
        return (value - vmin) / (vmax - vmin)

    for t in filtered:
        time_score = norm_desc(t["hours_to_end"], min_hours, max_hours)
        vol_score = norm_asc(t["volume24hr"], min_vol, max_vol)
        liq_score = norm_asc(t["liquidity"], min_liq, max_liq)

        score = w_time * time_score + w_vol * vol_score + w_liq * liq_score

        t["score_time"] = round(time_score, 4)
        t["score_vol"] = round(vol_score, 4)
        t["score_liq"] = round(liq_score, 4)
        t["score"] = round(score, 4)

    # Sortuj: najwyższy score pierwszy (= najkrótszy czas do końca)
    filtered.sort(key=lambda t: t["score"], reverse=True)

    max_tokens = config.get("max_tokens", 50)
    result = filtered[:max_tokens]

    print(f"[INFO] Wybrano {len(result)} tokenów (max_tokens={max_tokens})")

    if result:
        print("\n[INFO] === TOP 5 OPPORTUNITIES ===")
        for i, t in enumerate(result[:5], 1):
            print(f"  {i}. {t['outcome']} @ {t['price']:.3f} | "
                  f"Ends: {t['hours_to_end']:.1f}h | "
                  f"Upside: {t['upside']*100:.1f}% | "
                  f"Score: {t['score']:.3f}")
            print(f"     {t['question'][:60]}...")
            print(f"     token_id: {t['token_id'][:20]}..." if t.get('token_id') else "     token_id: MISSING!")
        print()

    return result


def save_tokens(tokens: List[Dict[str, Any]], output_file: str) -> None:
    """Zapisz tokeny do pliku JSON"""
    # Zaokrąglij liczby dla czytelności
    for t in tokens:
        t["price"] = round(t["price"], 4)
        t["upside"] = round(t["upside"], 4)
        t["rr"] = round(t["rr"], 4)
        t["hours_to_end"] = round(t["hours_to_end"], 2)
        t["seconds_to_end"] = round(t["seconds_to_end"], 0)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(tokens, f, ensure_ascii=False, indent=2)
    
    print(f"[INFO] Zapisano {len(tokens)} tokenów do: {output_file}")


def main():
    print("=" * 60)
    print("HIGH PROBABILITY TOKEN SCANNER")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    config = load_config("config.json")
    normalize_price_range(config)
    
    markets = fetch_markets(config["gamma_markets_url"])
    
    if not markets:
        print("[ERROR] Nie pobrano żadnych rynków!")
        return
    
    tokens = build_token_list(markets, config)
    
    if not tokens:
        print("[WARN] Nie znaleziono tokenów spełniających kryteria cenowe")
        # Zapisz pustą listę
        save_tokens([], config.get("output_file", "opportunities.json"))
        return
    
    selected = sort_and_filter_tokens(tokens, config)
    
    output_file = config.get("output_file", "opportunities.json")
    save_tokens(selected, output_file)
    
    print("=" * 60)
    print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Found: {len(selected)} opportunities")
    print("=" * 60)


if __name__ == "__main__":
    main()
