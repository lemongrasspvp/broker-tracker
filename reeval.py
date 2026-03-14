"""
reeval.py
Post-run re-evaluation of tracked tickers.
Layer A: Free yfinance price refresh + momentum.
Layer B: One cheap Claude batch call for thesis evolution.
"""

import json
import os
import re
import time
import anthropic
import yfinance as yf
from datetime import datetime, timedelta

from broker_tracker import _load as load_tracker, _save as save_tracker
from enricher import fetch_stock_data, format_stock_data_for_prompt, resolve_ticker, guess_country
from memory import store_reeval

REEVAL_FILE = "./data/reeval.json"
STALE_DAYS = 10  # match dashboard threshold


# ── LAYER A: FREE PRICE REFRESH ──────────────────────────────────────────────

def refresh_prices() -> dict:
    """
    Refresh yfinance prices for all tracked tickers.
    Returns dict of active (non-IGNORE, non-stale) tickers with their data.
    """
    records = load_tracker()
    if not records:
        return {}

    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = (datetime.now() - timedelta(days=STALE_DAYS)).strftime("%Y-%m-%d")
    updated = False
    active = {}

    for r in records:
        ticker = r["ticker"]

        # Skip if already checked today
        if r.get("last_checked") == today:
            # Still include in active set if non-stale and non-IGNORE
            if r.get("action") != "IGNORE" and r.get("date", "") >= cutoff:
                active[ticker] = r
            continue

        try:
            stock = yf.Ticker(ticker)
            info = stock.info or {}
            price = info.get("currentPrice") or info.get("regularMarketPrice")

            if price:
                r["current_price"] = round(price, 2)
                if r.get("price_at_rec"):
                    r["return_pct"] = round(
                        ((price - r["price_at_rec"]) / r["price_at_rec"]) * 100, 1
                    )

                # 5-day momentum
                try:
                    hist = stock.history(period="5d")
                    if len(hist) >= 2:
                        p_start = hist["Close"].iloc[0]
                        p_end = hist["Close"].iloc[-1]
                        pct = ((p_end - p_start) / p_start) * 100
                        if pct > 2:
                            r["momentum"] = "UP"
                        elif pct < -2:
                            r["momentum"] = "DOWN"
                        else:
                            r["momentum"] = "FLAT"
                    else:
                        r["momentum"] = "FLAT"
                except Exception:
                    r["momentum"] = "FLAT"

                r["last_checked"] = today
                updated = True

            time.sleep(0.5)  # avoid yfinance throttle
        except Exception:
            continue

        # Include non-IGNORE, non-stale tickers in active set
        if r.get("action") != "IGNORE" and r.get("date", "") >= cutoff:
            active[ticker] = r

    if updated:
        save_tracker(records)

    return active


# ── LAYER B: CHEAP BATCH THESIS CHECK ────────────────────────────────────────

REEVAL_PROMPT = """For each ticker below, assess thesis evolution and action signal based on price action, fundamentals, and context since the recommendation.

EVOLUTION:
- STRENGTHENING: entry conditions being met (e.g., "wait for dips" and it dipped), fundamentals improved, or catalyst playing out
- STABLE: no material change to thesis
- WEAKENING: thesis breaking down, key assumptions failing, or price moved sharply against it

Note "prev_evolution" if shown — detect trend shifts (was STRENGTHENING now STABLE = momentum fading).

Set "alert" to true if any of:
- Price moved >10% in either direction since recommendation
- Entry conditions from the original thesis are now met
- A critical assumption appears broken
- Evolution shifted direction from previous re-eval

ACTION SIGNAL (be strict — only signal when evidence is clear):
- STRONG_BUY: thesis strengthening + price in/below entry zone flagged in verdict + score >= 7 + consensus buy/strong_buy. Multiple confirming signals.
- BUY: thesis strengthening + price below analyst target by >15% + score >= 6. At least 2 confirming signals.
- HOLD: default. No clear action, or mixed signals.
- TAKE_PROFIT: in profit (return > 0) AND edge is fading — any of: price hit/exceeded analyst target, momentum turning DOWN after big run (>25% gain), thesis no longer strengthening after extended rally, valuation stretched above peers, or the original catalyst has fully played out.
- SELL: thesis breaking down — any of: price down >10% AND thesis weakening, critical assumption broken (regulatory, earnings miss, management change), consensus shifted to sell/underperform, or evolution was WEAKENING for consecutive re-evals.

TICKERS:
{ticker_blocks}

Respond with ONLY this JSON (no markdown):
{{"tickers": [{{"ticker": "string", "evolution": "STRENGTHENING|STABLE|WEAKENING", "note": "max 120 chars explaining reasoning", "alert": false, "action_signal": "STRONG_BUY|BUY|HOLD|TAKE_PROFIT|SELL"}}]}}"""


def batch_reeval(active_tickers: dict, api_key: str) -> dict:
    """
    One Claude Sonnet call to evaluate thesis evolution for all active tickers.
    No web search — just financial data assessment. Very cheap (~$0.01 total).
    """
    if not active_tickers:
        return {}

    # Load previous reeval results for evolution trend detection
    prev_reeval = {}
    if os.path.exists(REEVAL_FILE):
        try:
            with open(REEVAL_FILE, "r") as f:
                prev_reeval = json.load(f)
        except Exception:
            pass

    # Build per-ticker blocks with enricher data
    blocks = []
    for ticker, rec in active_tickers.items():
        price_at = rec.get("price_at_rec")
        cur_price = rec.get("current_price")
        ret = rec.get("return_pct")
        momentum = rec.get("momentum", "FLAT")

        # Fetch fresh financial summary (free yfinance call)
        # Resolve ticker suffix for yfinance (e.g., NOD → NOD.OL)
        country = guess_country(ticker, "")
        resolved = resolve_ticker(ticker, country)
        data = fetch_stock_data(resolved, country)
        target = data.get("analyst_target", "N/A")
        pe = data.get("pe_ratio")
        pe_str = f"{pe:.1f}" if pe else "N/A"
        low52 = data.get("52w_low", "N/A")
        high52 = data.get("52w_high", "N/A")
        rec_str = data.get("recommendation", "N/A")

        # Use enricher price as fallback if tracker price is null
        if not cur_price and data.get("current_price"):
            cur_price = data["current_price"]

        price_str = f"{cur_price}" if cur_price else "N/A"
        ret_str = f"{ret:+.1f}%" if ret is not None else "N/A"
        at_str = f"{price_at}" if price_at else "N/A"

        # 5-day price change
        ytd = data.get("ytd_return")
        ytd_str = f"{ytd:+.1f}%" if ytd is not None else "N/A"

        # Previous evolution for trend detection
        prev_info = prev_reeval.get(ticker, {})
        prev_evo = prev_info.get("evolution", "N/A")
        prev_sig = prev_info.get("action_signal", "N/A")
        streak = prev_info.get("streak", 0)

        block = (
            f"---\n"
            f"{ticker}: {rec.get('action', 'N/A')} on {rec.get('date', 'N/A')} at {at_str} → now {price_str} ({ret_str})\n"
            f"  Score: {rec.get('score', 'N/A')}/10 | Momentum 5d: {momentum} | YTD: {ytd_str}\n"
            f"  Verdict: {rec.get('verdict', 'N/A')}\n"
            f"  Target: {target} | P/E: {pe_str} | 52w: {low52}–{high52} | Consensus: {rec_str}\n"
            f"  prev_evolution: {prev_evo} (streak: {streak}x) | prev_signal: {prev_sig}"
        )
        blocks.append(block)
        time.sleep(0.3)  # pace yfinance calls

    prompt = REEVAL_PROMPT.format(ticker_blocks="\n".join(blocks))

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        result = json.loads(raw)
        return {
            t["ticker"]: {
                "evolution": t.get("evolution", "STABLE"),
                "note": t.get("note", ""),
                "alert": t.get("alert", False),
                "action_signal": t.get("action_signal", "HOLD"),
            }
            for t in result.get("tickers", [])
        }
    except Exception as e:
        print(f"  [RE-EVAL] Claude call failed: {e}")
        return {}


# ── STORE RESULTS ─────────────────────────────────────────────────────────────

def store_reeval_results(results: dict, active_tickers: dict):
    """
    Save re-eval results to reeval.json and append to memory.json.
    """
    if not results:
        return

    now = datetime.now().isoformat()

    # Load existing reeval data
    existing = {}
    if os.path.exists(REEVAL_FILE):
        try:
            with open(REEVAL_FILE, "r") as f:
                existing = json.load(f)
        except Exception:
            pass

    # Update with new results
    for ticker, data in results.items():
        rec = active_tickers.get(ticker, {})
        prev = existing.get(ticker, {})

        # Streak: count consecutive same-evolution results
        new_evo = data["evolution"]
        if prev.get("evolution") == new_evo:
            streak = prev.get("streak", 1) + 1
        else:
            streak = 1

        # History: append and keep last 7
        history = prev.get("history", [])
        history.append({"evolution": new_evo, "date": now[:10]})
        history = history[-7:]

        # Track previous signal for change detection
        prev_signal = prev.get("action_signal", "HOLD")
        new_signal = data.get("action_signal", "HOLD")

        existing[ticker] = {
            "evolution": new_evo,
            "note": data["note"],
            "alert": data["alert"],
            "action_signal": new_signal,
            "prev_action_signal": prev_signal,
            "streak": streak,
            "history": history,
            "timestamp": now,
            "price_at_reeval": rec.get("current_price"),
            "return_pct": rec.get("return_pct"),
        }

        # Append to memory so future analyses see evolution
        store_reeval(
            ticker=ticker,
            evolution=new_evo,
            note=data["note"],
            price=rec.get("current_price"),
        )

    os.makedirs(os.path.dirname(REEVAL_FILE), exist_ok=True)
    with open(REEVAL_FILE, "w") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
