"""
memory.py
Stores and retrieves per-ticker analysis history so Claude has context
on what has previously been said about a stock.
"""

import json
import os
from datetime import datetime

MEMORY_FILE = "./data/memory.json"


def _load() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save(data: dict):
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    with open(MEMORY_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_ticker_history(ticker: str, max_entries: int = 5) -> list[dict]:
    """Return the last N analysis summaries for a given ticker."""
    memory = _load()
    return memory.get(ticker.upper(), [])[-max_entries:]


def store_analysis(ticker: str, analysis_summary: dict):
    """
    Store a summary of an analysis for a ticker.
    Keeps last 20 entries per ticker.
    """
    memory = _load()
    key    = ticker.upper()
    if key not in memory:
        memory[key] = []

    memory[key].append({
        "date":              datetime.now().strftime("%Y-%m-%d"),
        "thesis_strength":   analysis_summary.get("thesis_strength"),
        "score":             analysis_summary.get("thesis_strength_score"),
        "action":            analysis_summary.get("recommended_action"),
        "verdict":           analysis_summary.get("one_line_verdict"),
        "price_at_analysis": analysis_summary.get("_price_at_analysis"),
    })

    # Keep last 20 per ticker
    memory[key] = memory[key][-20:]
    _save(memory)


def format_history_for_prompt(ticker: str) -> str:
    """Format ticker history as a readable block for the analysis prompt."""
    history = get_ticker_history(ticker)
    if not history:
        return f"No previous analyses found for {ticker}."

    lines = [f"PREVIOUS ANALYSES FOR {ticker.upper()} (oldest first):"]
    for h in history:
        price_note = f" | price was {h['price_at_analysis']}" if h.get("price_at_analysis") else ""
        lines.append(
            f"  {h['date']}: {h['thesis_strength']} ({h['score']}/10) → {h['action']}{price_note}"
        )
        if h.get("verdict"):
            lines.append(f"    Verdict: {h['verdict']}")

    return "\n".join(lines)


def store_reeval(ticker: str, evolution: str, note: str, price: float = None):
    """
    Store a re-evaluation entry for a ticker.
    Distinguished from full analyses by type='reeval'.
    """
    memory = _load()
    key = ticker.upper()
    if key not in memory:
        memory[key] = []

    memory[key].append({
        "date":              datetime.now().strftime("%Y-%m-%d"),
        "type":              "reeval",
        "thesis_strength":   None,
        "score":             None,
        "action":            None,
        "verdict":           f"{evolution} — {note}",
        "price_at_analysis": price,
    })

    memory[key] = memory[key][-20:]
    _save(memory)


def get_all_tracked_tickers() -> list[str]:
    return list(_load().keys())
