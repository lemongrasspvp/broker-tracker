"""
broker_tracker.py
Logs every recommendation with price at time of analysis.
On each run, updates current prices and calculates return since recommendation.
Builds a running accuracy scorecard.
"""

import json
import os
import yfinance as yf
from datetime import datetime

TRACKER_FILE = "./data/broker_track.json"


def _load() -> list[dict]:
    if os.path.exists(TRACKER_FILE):
        try:
            with open(TRACKER_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save(data: list):
    os.makedirs(os.path.dirname(TRACKER_FILE), exist_ok=True)
    with open(TRACKER_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def log_recommendation(ticker: str, action: str, thesis_strength: str,
                       score: int, verdict: str, current_price: float = None):
    """Log a new recommendation with the price at time of analysis."""
    records = _load()

    # Don't double-log the same ticker on the same day
    today = datetime.now().strftime("%Y-%m-%d")
    for r in records:
        if r["ticker"] == ticker.upper() and r["date"] == today:
            return

    records.append({
        "ticker":          ticker.upper(),
        "date":            today,
        "action":          action,
        "thesis_strength": thesis_strength,
        "score":           score,
        "verdict":         verdict,
        "price_at_rec":    current_price,
        "current_price":   current_price,
        "return_pct":      None,
        "last_checked":    today,
    })

    _save(records)


def update_prices():
    """Refresh current prices and recalculate returns for all tracked recommendations."""
    records = _load()
    if not records:
        return

    today = datetime.now().strftime("%Y-%m-%d")
    updated = False

    for r in records:
        if r.get("last_checked") == today:
            continue
        try:
            stock = yf.Ticker(r["ticker"])
            info  = stock.info or {}
            price = info.get("currentPrice") or info.get("regularMarketPrice")
            if price and r.get("price_at_rec"):
                r["current_price"] = round(price, 2)
                r["return_pct"]    = round(((price - r["price_at_rec"]) / r["price_at_rec"]) * 100, 1)
                r["last_checked"]  = today
                updated = True
        except Exception:
            continue

    if updated:
        _save(records)


def get_scorecard() -> str:
    """Return a formatted broker accuracy scorecard."""
    update_prices()
    records = _load()

    if not records:
        return "No recommendations tracked yet."

    lines = ["BROKER ACCURACY SCORECARD", "─" * 50]

    # Split into actionable and non-actionable
    actionable = [r for r in records if r["action"] in ("RESEARCH_FURTHER", "WAIT_FOR_DIPS")]
    monitored  = [r for r in records if r["action"] == "MONITOR"]
    ignored    = [r for r in records if r["action"] == "IGNORE"]

    def format_group(group: list, label: str) -> list[str]:
        if not group:
            return []
        out = [f"\n{label}:"]
        returns = [r["return_pct"] for r in group if r.get("return_pct") is not None]
        for r in sorted(group, key=lambda x: x["date"], reverse=True)[:10]:
            ret = f"{r['return_pct']:+.1f}%" if r.get("return_pct") is not None else "pending"
            price_note = f"  {r['price_at_rec']} → {r['current_price']}" if r.get("price_at_rec") else ""
            out.append(f"  {r['date']}  {r['ticker']:<10} {r['action']:<20} {ret}{price_note}")
        if returns:
            avg = sum(returns) / len(returns)
            positive = sum(1 for x in returns if x > 0)
            out.append(f"  Average return: {avg:+.1f}%  |  Win rate: {positive}/{len(returns)}")
        return out

    lines += format_group(actionable, "RESEARCHED / ACTED ON")
    lines += format_group(monitored,  "MONITORED")
    lines += format_group(ignored,    "IGNORED (opportunity cost check)")

    return "\n".join(lines)


def get_ticker_track_record(ticker: str) -> str:
    """Return past broker calls on a specific ticker."""
    records = [r for r in _load() if r["ticker"] == ticker.upper()]
    if not records:
        return f"No previous calls on {ticker}."

    lines = [f"BROKER TRACK RECORD ON {ticker.upper()}:"]
    for r in records:
        ret = f"  return: {r['return_pct']:+.1f}%" if r.get("return_pct") is not None else ""
        lines.append(f"  {r['date']}  {r['action']}  score={r['score']}/10{ret}")
        if r.get("verdict"):
            lines.append(f"    \"{r['verdict']}\"")
    return "\n".join(lines)
