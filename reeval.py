"""
reeval.py
Post-run re-evaluation of tracked tickers.
Layer A: Free yfinance price refresh.
Layer B: One cheap Claude batch call for thesis evolution.
"""

import json
import os
import re
import time
import anthropic
import yfinance as yf
from datetime import datetime

from broker_tracker import _load as load_tracker, _save as save_tracker
from enricher import fetch_stock_data, format_stock_data_for_prompt, resolve_ticker, guess_country
from memory import store_reeval

REEVAL_FILE = "./data/reeval.json"

# Action signals worth re-checking. HOLD and missing signals are skipped on
# subsequent passes — except first-time tickers, which always get one
# bootstrap re-eval so they can earn a real signal.
ACTIONABLE_SIGNALS = {"STRONG_BUY", "BUY", "TAKE_PROFIT", "SELL"}


# ── LAYER A: FREE PRICE REFRESH ──────────────────────────────────────────────

def refresh_prices() -> dict:
    """
    Refresh yfinance prices for all tracked tickers.
    Returns dict of non-IGNORE tickers with their data.
    """
    records = load_tracker()
    if not records:
        return {}

    today = datetime.now().strftime("%Y-%m-%d")
    updated = False
    active = {}

    for r in records:
        ticker = r["ticker"]

        # Skip if already checked today
        if r.get("last_checked") == today:
            if r.get("action") != "IGNORE":
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

                # 5-day price action as a raw number — Claude reasons about it
                # fluidly alongside return_pct / YTD / 52w range. No bucketing.
                try:
                    hist = stock.history(period="5d")
                    if len(hist) >= 2:
                        p_start = hist["Close"].iloc[0]
                        p_end   = hist["Close"].iloc[-1]
                        r["5d_return_pct"] = round(((p_end - p_start) / p_start) * 100, 1)
                    else:
                        r["5d_return_pct"] = None
                except Exception:
                    r["5d_return_pct"] = None

                r["last_checked"] = today
                updated = True

            time.sleep(0.5)  # avoid yfinance throttle
        except Exception:
            continue

        if r.get("action") != "IGNORE":
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

ACTION SIGNAL — apply the judgment of an experienced analyst managing real money. The numbers in each ticker block (return since rec, 5-day move, YTD, target, P/E, 52-week range, streak) are inputs to your reasoning, not thresholds to mechanically check. Read the whole picture: would you actually act on this signal today? Default to HOLD whenever the situation is mixed or unclear. Be willing to call SELL or TAKE_PROFIT — those signals exist for a reason.

- STRONG_BUY: rare. The setup is unusually compelling RIGHT NOW. Thesis is actively playing out, the entry is still attractive (you are not chasing a stock that has already moved most of the way), and conviction is high across multiple independent inputs — fundamentals, valuation, catalyst, sentiment. If you are reaching to justify it, downgrade to BUY.

- BUY: the trade is attractive for a NEW position TODAY. The bar is not "thesis intact + still below analyst target" — it is "if a friend asked, would I tell them to enter at this price?". A stock that has already run up substantially since the recommendation usually fails this test even if the long-term thesis still holds: the entry the broker proposed has been spent, and chasing is not a recommendation. Exception: a fresh independent catalyst since the original call, or the stock has come back into the broker's stated entry zone after a pullback.

- HOLD: the default. Use generously. Anything mixed, anything where the move is largely played out but nothing is broken, anything where you don't have strong conviction in a direction — HOLD. Most stocks on most days are HOLD.

- TAKE_PROFIT: the trade has worked and the edge is fading. Read the situation rather than waiting for a precise % move. Triggers fluidly when several of these are true: price has reached or stretched past the analyst target, momentum has rolled over after a meaningful run, the original catalyst has occurred, valuation has expanded relative to peers, or the thesis no longer reads as STRENGTHENING after an extended rally. New buyers should not enter; existing holders should consider trimming.

- SELL: the original thesis is no longer valid — not just stalled, but broken. A critical assumption has failed (earnings miss, regulatory action, management change), consensus has turned negative, or the stock is in a sustained downtrend with WEAKENING fundamentals over multiple re-evals. Distinguished from TAKE_PROFIT by this question: would a fresh analyst recommend this today? "No, avoid entirely" → SELL. "Yes, but not at this price" → TAKE_PROFIT.

TICKERS:
{ticker_blocks}

Respond with ONLY this JSON (no markdown):
{{"tickers": [{{"ticker": "string", "evolution": "STRENGTHENING|STABLE|WEAKENING", "note": "max 120 chars explaining reasoning", "alert": false, "action_signal": "STRONG_BUY|BUY|HOLD|TAKE_PROFIT|SELL"}}]}}"""


BATCH_SIZE = 30  # tickers per Claude call to avoid output truncation


def batch_reeval(active_tickers: dict, api_key: str) -> dict:
    """
    Evaluate thesis evolution for all active tickers in batches.
    Splits into groups of BATCH_SIZE to avoid output truncation.
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

    # Gate: re-check a ticker if any of:
    #   - no prior reeval entry yet (bootstrap pass)
    #   - last action_signal is actionable (BUY/SELL/etc — situation may shift)
    #   - broker's recommendation was WAIT_FOR_DIPS (entry condition may trigger)
    to_check = {}
    skipped = 0
    for ticker, rec in active_tickers.items():
        prev = prev_reeval.get(ticker)
        is_bootstrap   = prev is None
        has_signal     = prev is not None and prev.get("action_signal") in ACTIONABLE_SIGNALS
        waiting_for_dip = rec.get("action") == "WAIT_FOR_DIPS"
        if is_bootstrap or has_signal or waiting_for_dip:
            to_check[ticker] = rec
        else:
            skipped += 1
    if skipped:
        print(f"  [RE-EVAL] Skipping {skipped} HOLD ticker(s); checking {len(to_check)}")

    # Build per-ticker blocks with enricher data
    ticker_blocks = {}  # ticker -> block string
    for ticker, rec in to_check.items():
        price_at  = rec.get("price_at_rec")
        cur_price = rec.get("current_price")
        ret       = rec.get("return_pct")
        ret_5d    = rec.get("5d_return_pct")

        # Resolve ticker suffix for yfinance (e.g., NOD → NOD.OL)
        country = guess_country(ticker, "")
        resolved = resolve_ticker(ticker, country)
        data = fetch_stock_data(resolved, country)
        target = data.get("analyst_target", "N/A")
        pe = data.get("pe_ratio")
        try:
            pe_str = f"{float(pe):.1f}" if pe is not None else "N/A"
        except (TypeError, ValueError):
            pe_str = "N/A"
        low52 = data.get("52w_low", "N/A")
        high52 = data.get("52w_high", "N/A")
        rec_str = data.get("recommendation", "N/A")

        # Use enricher price as fallback if tracker price is null
        if not cur_price and data.get("current_price"):
            cur_price = data["current_price"]

        price_str = f"{cur_price}" if cur_price else "N/A"
        ret_str = f"{ret:+.1f}%" if ret is not None else "N/A"
        at_str = f"{price_at}" if price_at else "N/A"

        ytd = data.get("ytd_return")
        ytd_str    = f"{ytd:+.1f}%"    if ytd    is not None else "N/A"
        ret_5d_str = f"{ret_5d:+.1f}%" if ret_5d is not None else "N/A"

        # Previous evolution for trend detection
        prev_info = prev_reeval.get(ticker, {})
        prev_evo = prev_info.get("evolution", "N/A")
        prev_sig = prev_info.get("action_signal", "N/A")
        streak = prev_info.get("streak", 0)

        block = (
            f"---\n"
            f"{ticker}: {rec.get('action', 'N/A')} on {rec.get('date', 'N/A')} at {at_str} → now {price_str} ({ret_str})\n"
            f"  Score: {rec.get('score', 'N/A')}/10 | 5d: {ret_5d_str} | YTD: {ytd_str}\n"
            f"  Verdict: {rec.get('verdict', 'N/A')}\n"
            f"  Target: {target} | P/E: {pe_str} | 52w: {low52}–{high52} | Consensus: {rec_str}\n"
            f"  prev_evolution: {prev_evo} (streak: {streak}x) | prev_signal: {prev_sig}"
        )
        ticker_blocks[ticker] = block
        time.sleep(0.3)  # pace yfinance calls

    # Split into batches
    tickers_list = list(ticker_blocks.keys())
    batches = [tickers_list[i:i + BATCH_SIZE] for i in range(0, len(tickers_list), BATCH_SIZE)]
    print(f"  [RE-EVAL] {len(tickers_list)} tickers in {len(batches)} batch(es)")

    all_results = {}
    client = anthropic.Anthropic(api_key=api_key)

    for batch_idx, batch_tickers in enumerate(batches):
        batch_blocks = [ticker_blocks[t] for t in batch_tickers]
        prompt = REEVAL_PROMPT.format(ticker_blocks="\n".join(batch_blocks))

        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)

            if response.stop_reason == "max_tokens":
                print(f"  [RE-EVAL] WARNING: Batch {batch_idx + 1} truncated — some tickers may be missing")

            result = json.loads(raw)
            for t in result.get("tickers", []):
                all_results[t["ticker"]] = {
                    "evolution": t.get("evolution", "STABLE"),
                    "note": t.get("note", ""),
                    "alert": t.get("alert", False),
                    "action_signal": t.get("action_signal", "HOLD"),
                }

            print(f"  [RE-EVAL] Batch {batch_idx + 1}/{len(batches)} done — {len(result.get('tickers', []))} tickers")
            if batch_idx < len(batches) - 1:
                time.sleep(2)  # brief pause between batches

        except Exception as e:
            print(f"  [RE-EVAL] Batch {batch_idx + 1} failed: {e}")
            continue

    return all_results


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
