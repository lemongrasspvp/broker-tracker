#!/usr/bin/env python3
"""dashboard.py — Generate conviction dashboard HTML.
Usage: python3 dashboard.py && open dashboard.html
"""

import json
import os
import re
import html as html_module
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

DATA_DIR     = os.getenv("DATA_DIR",   "./data")
ANALYSES_DIR = os.getenv("OUTPUT_DIR", "./analyses")
MEMORY_FILE  = os.path.join(DATA_DIR, "memory.json")
TRACKER_FILE = os.path.join(DATA_DIR, "broker_track.json")
OUTPUT_FILE  = "dashboard.html"

NEW_THRESHOLD_HOURS  = 48
STALE_THRESHOLD_DAYS = 10


# ── HELPERS ───────────────────────────────────────────────────────────────────

def normalise_score(raw) -> float:
    if raw is None:
        return 0.0
    v = float(raw)
    return round(v / 10.0, 1) if v > 10 else round(v, 1)


def parse_email_date(date_str: str):
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def relative_time(dt) -> str:
    if dt is None:
        return ""
    now  = datetime.now(timezone.utc)
    diff = now - dt.astimezone(timezone.utc)
    secs = diff.total_seconds()
    if secs < 0:
        return "just now"
    mins  = int(secs // 60)
    hours = int(secs // 3600)
    days  = int(secs // 86400)
    if mins < 60:
        return f"{max(1, mins)} min ago"
    if hours < 24:
        return f"{hours} hr ago"
    return f"{days} days ago"


def esc(s) -> str:
    return html_module.escape(str(s or ""), quote=True)


# ── DATA LOADING ──────────────────────────────────────────────────────────────

def load_memory() -> dict:
    if not os.path.exists(MEMORY_FILE):
        return {}
    with open(MEMORY_FILE) as f:
        return json.load(f)


def load_tracker() -> dict:
    if not os.path.exists(TRACKER_FILE):
        return {}
    with open(TRACKER_FILE) as f:
        records = json.load(f)
    by_ticker: dict = {}
    for r in records:
        t  = r.get("ticker", "")
        ex = by_ticker.get(t)
        if ex is None or r.get("date", "") >= ex.get("date", ""):
            by_ticker[t] = r
    return by_ticker


def load_reeval() -> dict:
    reeval_file = os.path.join(DATA_DIR, "reeval.json")
    if not os.path.exists(reeval_file):
        return {}
    try:
        with open(reeval_file) as f:
            return json.load(f)
    except Exception:
        return {}


def extract_signals(stock: dict) -> list:
    tags = []
    insider = (stock.get("insider_read") or "").lower()
    if any(w in insider for w in ["bought", "purchase", "exercise", "acquiring"]):
        tags.append("insider bought")
    elif any(w in insider for w in ["sold", "sale", "selling", "disposed"]):
        tags.append("insider sold")

    short = (stock.get("short_interest_read") or "").lower()
    if any(w in short for w in ["high", "elevated", "significant", "heavy"]):
        tags.append("high short interest")
    elif any(w in short for w in ["low", "minimal", "negligible"]):
        tags.append("low short interest")

    forum = (stock.get("forum_sentiment") or "").lower()
    if any(w in forum for w in ["bullish", "positive", "optimistic", "buying"]):
        tags.append("forum bullish")
    elif any(w in forum for w in ["bearish", "negative", "pessimistic", "selling", "cautious"]):
        tags.append("forum bearish")

    catalyst = (stock.get("catalyst_note") or "").lower()
    if any(w in catalyst for w in ["earnings", "results", "report", "q1", "q2", "q3", "q4"]):
        m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*", catalyst)
        month = m.group(0).capitalize() if m else ""
        tags.append("earnings " + month if month else "earnings upcoming")

    return tags[:3]


def load_signals_from_analyses():
    """
    Returns:
      ticker_data  – dict of ticker -> rich info dict
      news_emails  – list of dicts for low-quality/forwarded emails
    """
    if not os.path.exists(ANALYSES_DIR):
        return {}, []

    files = sorted(
        [f for f in os.listdir(ANALYSES_DIR) if f.endswith(".json")],
        reverse=True,   # newest filename first
    )

    ticker_data: dict = {}
    news_emails: list = []

    for fname in files:
        path = os.path.join(ANALYSES_DIR, fname)
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue

        email_dt  = parse_email_date(data.get("_email_date", ""))
        subject   = (data.get("_email_subject") or "").replace("\r\n", " ").replace("\n", " ").strip()
        quality   = data.get("overall_email_quality", "")
        qual_note = (data.get("overall_email_quality_note") or "")
        is_news   = quality == "LOW"

        stocks            = data.get("stocks", [])
        tickers_in_email  = []

        for stock in stocks:
            t = (stock.get("ticker") or "").upper().strip()
            if not t:
                continue
            tickers_in_email.append(t)
            if t not in ticker_data:
                ticker_data[t] = {
                    "signals":           extract_signals(stock),
                    "email_dt":          email_dt,
                    "email_subject":     subject,
                    "investment_thesis": (stock.get("investment_thesis") or ""),
                    "key_claims":        stock.get("key_claims") or [],
                    "company_name":      (stock.get("company_name") or ""),
                    "bull_case":         (stock.get("bull_case") or ""),
                    "bear_case":         (stock.get("bear_case") or ""),
                }

        if is_news and tickers_in_email:
            # Collect per-ticker details for news popup
            ticker_details = []
            for stock in stocks:
                t = (stock.get("ticker") or "").upper().strip()
                if not t:
                    continue
                strength = (stock.get("thesis_strength") or "").upper()
                if strength in ("STRONG", "MODERATE"):
                    sentiment = "BULLISH"
                elif strength in ("WEAK",):
                    sentiment = "BEARISH"
                else:
                    sentiment = "NEUTRAL"
                ticker_details.append({
                    "ticker":           t,
                    "company_name":     (stock.get("company_name") or ""),
                    "investment_thesis": (stock.get("investment_thesis") or ""),
                    "sentiment":        sentiment,
                })
            news_emails.append({
                "subject":        subject,
                "email_dt":       email_dt,
                "quality_note":   qual_note,
                "tickers":        tickers_in_email,
                "ticker_details": ticker_details,
            })

    news_emails.sort(
        key=lambda e: e["email_dt"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return ticker_data, news_emails


# ── AGGREGATION ───────────────────────────────────────────────────────────────

def build_rows(ticker_data: dict):
    memory  = load_memory()
    tracker = load_tracker()
    reeval  = load_reeval()
    now     = datetime.now(timezone.utc)

    rows = []
    for ticker, history in memory.items():
        if not history:
            continue
        # Skip re-eval entries when picking latest real analysis
        analyses = [h for h in history if h.get("type") != "reeval"]
        if not analyses:
            continue
        latest   = analyses[-1]
        previous = analyses[-2] if len(analyses) >= 2 else None

        norm_score = normalise_score(latest.get("score", 0))
        prev_norm  = normalise_score(previous["score"]) if previous else None

        if prev_norm is None:
            trend = "→"
        elif norm_score > prev_norm + 0.4:
            trend = "↑"
        elif norm_score < prev_norm - 0.4:
            trend = "↓"
        else:
            trend = "→"

        info      = ticker_data.get(ticker.upper(), {})
        email_dt  = info.get("email_dt")
        age_label = relative_time(email_dt)

        hours_old = (
            (now - email_dt.astimezone(timezone.utc)).total_seconds() / 3600
            if email_dt else 9999
        )
        is_new   = hours_old < NEW_THRESHOLD_HOURS
        is_stale = hours_old > STALE_THRESHOLD_DAYS * 24

        track      = tracker.get(ticker, {})
        return_pct = track.get("return_pct")

        action   = latest.get("action", "")
        strength = latest.get("thesis_strength", "")

        reeval_info = reeval.get(ticker, {})

        if is_new:
            section = "NEW"
        elif is_stale and reeval_info.get("action_signal") == "STRONG_BUY":
            section = "HIGH CONVICTION"
        elif is_stale:
            section = "OLDER"
        elif norm_score > 7:
            section = "HIGH CONVICTION"
        elif norm_score >= 5:
            section = "WATCHLIST"
        else:
            section = "IGNORE"
        rows.append({
            "ticker":            ticker,
            "score":             norm_score,
            "strength":          strength,
            "action":            action,
            "trend":             trend,
            "verdict":           latest.get("verdict", ""),
            "age_label":         age_label,
            "return_pct":        return_pct,
            "signals":           info.get("signals", []),
            "section":           section,
            "is_stale":          is_stale,
            "is_new":            is_new,
            "hours_old":         hours_old,
            "email_subject":     info.get("email_subject", ""),
            "investment_thesis": info.get("investment_thesis", ""),
            "key_claims":        info.get("key_claims", []),
            "company_name":      info.get("company_name", ""),
            "bull_case":         info.get("bull_case", ""),
            "bear_case":         info.get("bear_case", ""),
            "evolution":         reeval_info.get("evolution"),
            "evolution_note":    reeval_info.get("note", ""),
            "evolution_alert":   reeval_info.get("alert", False),
            "action_signal":     reeval_info.get("action_signal", ""),
            "prev_action_signal": reeval_info.get("prev_action_signal", ""),
            "evolution_history": reeval_info.get("history", []),
            "streak":            reeval_info.get("streak", 0),
        })

    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


# ── RENDER HELPERS ────────────────────────────────────────────────────────────

ACTION_LABELS = {
    "RESEARCH_FURTHER": "RESEARCH",
    "MONITOR":          "MONITOR",
    "WAIT_FOR_DIPS":    "WAIT FOR DIP",
    "IGNORE":           "IGNORE",
}
STRENGTH_COLORS = {
    "STRONG":   "#22c55e",
    "MODERATE": "#f59e0b",
    "WEAK":     "#ef4444",
    "UNCLEAR":  "#94a3b8",
}
ACTION_COLORS = {
    "RESEARCH_FURTHER": "#6366f1",
    "MONITOR":          "#3b82f6",
    "WAIT_FOR_DIPS":    "#f59e0b",
    "IGNORE":           "#94a3b8",
}
CLAIM_ICONS = {
    "CREDIBLE":     ("✓", "#22c55e"),
    "QUESTIONABLE": ("⚠", "#f59e0b"),
    "UNVERIFIED":   ("⚠", "#f59e0b"),
    "FALSE":        ("✗", "#ef4444"),
    "MISLEADING":   ("✗", "#ef4444"),
}
SECTION_ORDER = ["NEW", "HIGH CONVICTION", "WATCHLIST", "IGNORE", "OLDER"]
SECTION_META  = {
    "NEW":            ("🆕", "Last 48 hours",     False),
    "HIGH CONVICTION":("◆",  "Score > 7.0",       False),
    "WATCHLIST":      ("◇",  "Score 5.0 – 7.0",   False),
    "IGNORE":         ("–",  "Score < 5.0",        True),
    "OLDER":          ("⏷",  "> 10 days old",      True),
}


def render_popup(r: dict) -> str:
    subject   = esc(r.get("email_subject", ""))
    thesis    = esc(r.get("investment_thesis", ""))

    claims_html = ""
    for c in (r.get("key_claims") or [])[:5]:
        assessment = (c.get("assessment") or "").upper()
        icon, color = CLAIM_ICONS.get(assessment, ("·", "#94a3b8"))
        claim_text  = esc(c.get("claim") or "")
        claims_html += (
            '<div class="tt-claim">'
            f'<span class="tt-icon" style="color:{color}">{icon}</span>'
            f'<span>{claim_text}</span>'
            '</div>'
        )

    bull = esc(r.get("bull_case", ""))
    bear = esc(r.get("bear_case", ""))
    bb_html = ""
    if bull or bear:
        bb_html = (
            '<div class="tt-bb">'
            + (f'<div class="tt-bull">▲ {bull}</div>' if bull else "")
            + (f'<div class="tt-bear">▼ {bear}</div>' if bear else "")
            + '</div>'
        )

    parts = ['<div class="popup">']
    if subject:
        parts.append(f'<div class="tt-subject">{subject}</div>')
    if thesis:
        parts.append(f'<div class="tt-thesis">{thesis}</div>')
    if claims_html:
        parts.append(f'<div class="tt-claims">{claims_html}</div>')
    if bb_html:
        parts.append(bb_html)
    parts.append('</div>')
    return "".join(parts)


def render_card(r: dict) -> str:
    score    = r["score"]
    bar_pct  = min(100, score * 10)
    is_stale = r["is_stale"]

    bar_color    = "#94a3b8" if is_stale else STRENGTH_COLORS.get(r["strength"], "#94a3b8")
    card_opacity = "0.55"   if is_stale else "1"
    stale_html   = '<span class="stale-badge">stale</span>' if is_stale else ""

    action_label = ACTION_LABELS.get(r["action"], r["action"])
    action_color = ACTION_COLORS.get(r["action"], "#94a3b8")
    trend_color  = {"↑": "#22c55e", "↓": "#ef4444", "→": "#94a3b8"}.get(r["trend"], "#94a3b8")

    # "NEW" tag for recently analyzed tickers (within 24h)
    new_tag_html = '<span class="new-tag">NEW</span>' if r.get("is_new") else ""

    # Evolution badge with hover popup
    evo = r.get("evolution")
    if evo:
        _evo_colors = {"STRENGTHENING": "#22c55e", "STABLE": "#f59e0b", "WEAKENING": "#ef4444"}
        _evo_icons  = {"STRENGTHENING": "▲", "STABLE": "●", "WEAKENING": "▼"}
        evo_color = _evo_colors.get(evo, "#94a3b8")
        evo_icon  = _evo_icons.get(evo, "·")
        evo_note  = esc(r.get("evolution_note", ""))
        alert_cls = " evolution-alert" if r.get("evolution_alert") else ""
        evo_html = (
            f'<span class="evo-wrapper">'
            f'<span class="evolution-badge{alert_cls}" style="color:{evo_color}">{evo_icon} {evo}</span>'
            f'<span class="evo-popup">{evo_note}</span>'
            f'</span>'
        ) if evo_note else f'<span class="evolution-badge{alert_cls}" style="color:{evo_color}">{evo_icon} {evo}</span>'
    else:
        evo_html = ""

    # Action signal pill (5 levels)
    _signal_map = {
        "STRONG_BUY":  ("STRONG BUY",   "action-signal-strong-buy"),
        "BUY":         ("BUY",          "action-signal-buy"),
        "TAKE_PROFIT": ("TAKE PROFIT",  "action-signal-take-profit"),
        "SELL":        ("SELL",         "action-signal-sell"),
    }
    action_signal = r.get("action_signal", "")
    prev_signal = r.get("prev_action_signal", "")
    sig_info = _signal_map.get(action_signal)
    signal_changed = sig_info and action_signal != prev_signal and action_signal != "HOLD"
    new_cls = " signal-new" if signal_changed else ""
    signal_html = f'<span class="action-signal {sig_info[1]}{new_cls}">{sig_info[0]}</span>' if sig_info else ""

    # Mini bars evolution timeline (last 7 re-evals)
    evo_history = r.get("evolution_history", [])
    bars_html = ""
    if evo_history:
        _bar_map = {
            "STRENGTHENING": ("evo-bar-up", "18"),
            "STABLE":        ("evo-bar-stable", "10"),
            "WEAKENING":     ("evo-bar-down", "6"),
        }
        bar_parts = []
        for h in evo_history:
            cls, ht = _bar_map.get(h.get("evolution", "STABLE"), ("evo-bar-stable", "10"))
            bar_parts.append(f'<div class="evo-bar {cls}" style="height:{ht}px"></div>')
        bars_html = f'<div class="evo-bars">{"".join(bar_parts)}</div>'

    pills_html  = "".join(f'<span class="pill">{esc(sig)}</span>' for sig in r["signals"])
    glow        = 'style="box-shadow:0 0 0 2px #6366f166,0 2px 8px rgba(0,0,0,.1)"' if r["hours_old"] < 6 else ""
    t           = esc(r["ticker"])
    comp        = esc(r.get("company_name", ""))
    popup_html = render_popup(r)

    out = ['<div class="card" data-ticker="' + t + '" style="opacity:' + card_opacity + '" ' + glow + '>']
    out.append(popup_html)
    out.append('<div class="card-header">')
    out.append('<div class="card-title-group">')
    out.append(f'<span class="ticker">{t}</span>')
    if new_tag_html:
        out.append(new_tag_html)
    if comp:
        out.append(f'<span class="company-name">{comp}</span>')
    out.append('</div>')
    if stale_html:
        out.append(stale_html)
    out.append(f'<span class="action-badge" style="background:{action_color}18;color:{action_color};border:1px solid {action_color}44">{action_label}</span>')
    out.append(f'<span class="age">{esc(r["age_label"])}</span>')
    out.append(f'<button class="star-btn" data-ticker="{t}" title="Track this stock">☆</button>')
    out.append('</div>')  # card-header
    out.append('<div class="score-row">')
    out.append(f'<div class="bar-track"><div class="bar-fill" style="width:{bar_pct}%;background:{bar_color}"></div></div>')
    out.append(f'<span class="score-label">{score:.1f}</span>')
    out.append(f'<span class="strength-label" style="color:{bar_color}">{esc(r["strength"])}</span>')
    out.append(f'<span class="trend" style="color:{trend_color}">{r["trend"]}</span>')
    out.append('</div>')  # score-row
    out.append(f'<p class="verdict">{esc(r["verdict"])}</p>')
    out.append(f'<div class="signal-row"><div class="signal-left">{pills_html}</div><div class="signal-right">{evo_html}{signal_html}{bars_html}</div></div>')
    out.append('</div>')  # card
    return "".join(out)


def _first_sentence(text: str, max_len: int = 140) -> str:
    """Extract the first sentence, capped at max_len without trailing '…'."""
    if not text:
        return ""
    # Find sentence-ending period (followed by space + uppercase, or end of string)
    # This avoids splitting on abbreviations like "U.S." or "Dr."
    import re as _re
    m = _re.search(r'[.!?](?:\s+[A-Z]|\s*$)', text[:max_len])
    if m and m.start() > 20:
        return text[:m.start() + 1]
    # No clean sentence boundary — break at last space before max_len
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rfind(" ")
    if cut < 40:
        cut = max_len
    return text[:cut] + "."


SENTIMENT_STYLE = {
    "BULLISH":  ("▲ Bullish",  "#22c55e"),
    "BEARISH":  ("▼ Bearish",  "#ef4444"),
    "NEUTRAL":  ("— Neutral",  "#94a3b8"),
}


def render_news_popup(e: dict) -> str:
    """Popup with in-depth per-ticker summary + bullish/bearish indicator."""
    subject = esc(e.get("subject", ""))

    parts = ['<div class="popup">']
    if subject:
        parts.append(f'<div class="tt-subject">{subject}</div>')

    for td in (e.get("ticker_details") or []):
        ticker    = esc(td.get("ticker", ""))
        cname     = esc(td.get("company_name", ""))
        thesis    = esc(td.get("investment_thesis", ""))
        sentiment = td.get("sentiment", "NEUTRAL")
        label_text, label_color = SENTIMENT_STYLE.get(sentiment, SENTIMENT_STYLE["NEUTRAL"])

        parts.append('<div class="news-popup-ticker">')
        name_label = f'{ticker} — {cname}' if cname else ticker
        parts.append(
            f'<div class="news-popup-ticker-header">'
            f'{name_label}'
            f'<span class="sentiment-pill" style="color:{label_color};border-color:{label_color}44;background:{label_color}15">{label_text}</span>'
            f'</div>'
        )
        if thesis:
            parts.append(f'<div class="tt-thesis" style="margin-bottom:4px">{thesis}</div>')
        parts.append('</div>')

    parts.append('</div>')
    return "".join(parts)


def render_news_card(e: dict) -> str:
    subject = esc(e.get("subject", "No subject"))
    age     = esc(relative_time(e.get("email_dt")))

    # Build a short summary from the first ticker's thesis
    details = e.get("ticker_details") or []
    first_thesis = details[0].get("investment_thesis", "") if details else ""
    summary = esc(_first_sentence(first_thesis, 140))

    # Ticker pills with sentiment colors
    ticker_pills = []
    for td in details:
        t = esc(td.get("ticker", ""))
        sentiment = td.get("sentiment", "NEUTRAL")
        _, color = SENTIMENT_STYLE.get(sentiment, SENTIMENT_STYLE["NEUTRAL"])
        ticker_pills.append(
            f'<span class="pill news-ticker-pill" data-ticker="{t}"'
            f' style="color:{color};border-color:{color}44">{t}</span>'
        )
    ticks = "".join(ticker_pills)

    popup_html = render_news_popup(e)

    out = ['<div class="card news-card">']
    out.append(popup_html)
    out.append('<div class="card-header">')
    out.append(f'<span class="news-subject">{subject}</span>')
    out.append(f'<span class="age">{age}</span>')
    out.append('</div>')
    if summary:
        out.append(f'<p class="verdict">{summary}</p>')
    if ticks:
        out.append(f'<div class="signal-row">{ticks}</div>')
    out.append('</div>')
    return "".join(out)


# ── HTML ASSEMBLY ─────────────────────────────────────────────────────────────

def build_html(rows: list, news_emails: list) -> str:
    by_section: dict = {s: [] for s in SECTION_ORDER}
    for r in rows:
        by_section[r["section"]].append(r)

    generated  = datetime.now().strftime("%d %b %Y %H:%M")
    total      = len(rows)
    new_count  = len(by_section.get("NEW", []))
    new_notice = (
        f'<span class="new-notice">{new_count} new</span>'
        if new_count else ""
    )

    # JS data for tracking section
    js_data = {}
    for r in rows:
        js_data[r["ticker"]] = {
            "score":        r["score"],
            "strength":     r["strength"],
            "action":       r["action"],
            "verdict":      r["verdict"],
            "trend":        r["trend"],
            "age_label":    r["age_label"],
            "company_name": r.get("company_name", ""),
        }
    js_data_json = json.dumps(js_data, ensure_ascii=False)

    # News section
    news_html = ""
    if news_emails:
        ncards   = "".join(render_news_card(e) for e in news_emails)
        news_html = (
            '<details open class="section-details">'
            '<summary class="section-header">'
            '<span class="section-chevron">▶</span>'
            '<span class="section-icon">📰</span>'
            '<span class="section-title">NEWS UPDATES</span>'
            f'<span class="count">{len(news_emails)}</span>'
            '<span class="section-sub">News forwards — no actionable thesis</span>'
            '</summary>'
            f'<div class="section-body">{ncards}</div>'
            '</details>'
        )

    # Stock sections
    stock_html = ""
    for section in SECTION_ORDER:
        cards = by_section.get(section, [])
        if not cards:
            continue
        icon, subtitle, collapsed = SECTION_META[section]
        open_attr  = "" if collapsed else "open"
        cards_html = "".join(render_card(c) for c in cards)
        stock_html += (
            f'<details {open_attr} class="section-details">'
            '<summary class="section-header">'
            '<span class="section-chevron">▶</span>'
            f'<span class="section-icon">{icon}</span>'
            f'<span class="section-title">{section}</span>'
            f'<span class="count">{len(cards)}</span>'
            f'<span class="section-sub">{subtitle}</span>'
            '</summary>'
            f'<div class="section-body">{cards_html}</div>'
            '</details>'
        )

    # Assemble final HTML
    parts = []
    parts.append('<!DOCTYPE html>\n<html lang="en">\n<head>\n')
    parts.append('<meta charset="UTF-8">\n')
    parts.append('<meta name="viewport" content="width=device-width,initial-scale=1">\n')
    parts.append('<title>Conviction Dashboard</title>\n')
    parts.append('<style>\n')
    parts.append(_CSS)
    parts.append('\n</style>\n</head>\n<body>\n')

    # Top bar
    parts.append('<div class="top-bar">\n')
    parts.append('  <h1>Conviction Dashboard</h1>\n')
    if new_notice:
        parts.append(f'  {new_notice}\n')
    parts.append('  <input id="search-filter" type="text" placeholder="Filter stocks…" autocomplete="off" spellcheck="false">\n')
    parts.append(f'  <span class="meta">Updated {generated} &middot; {total} stocks</span>\n')
    parts.append('</div>\n')

    # Main layout: stocks left, sidebar right
    parts.append('<div class="main-layout">\n')
    parts.append('<div class="main-content">\n')
    parts.append(stock_html)
    parts.append('\n</div>\n')

    # Right sidebar: Tracking + News
    parts.append('<div class="sidebar">\n')
    parts.append(_TRACKING_HTML)
    parts.append('\n')
    parts.append(news_html)
    parts.append('\n</div>\n')
    parts.append('</div>\n')

    # JS
    parts.append('<script>\nconst STOCK_DATA = ')
    parts.append(js_data_json)
    parts.append(';\n')
    parts.append(_JS)
    parts.append('\n</script>\n</body>\n</html>')

    return "".join(parts)


# ── CSS ───────────────────────────────────────────────────────────────────────

_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, sans-serif;
  background: #f1f5f9;
  color: #1e293b;
  padding: 20px 16px 40px;
  max-width: 1200px;
  margin: 0 auto;
}

/* ── Top bar ── */
.top-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 16px;
}
h1 { font-size: 1.05rem; font-weight: 700; letter-spacing: .02em; color: #0f172a; }
#search-filter {
  margin-left: auto;
  font-size: .7rem;
  padding: 4px 10px;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  width: 160px;
  outline: none;
  color: #1e293b;
  background: #fff;
}
#search-filter:focus { border-color: #3b82f6; box-shadow: 0 0 0 2px #3b82f620; }
#search-filter::placeholder { color: #94a3b8; }
.meta { font-size: .7rem; color: #94a3b8; }
.new-notice {
  font-size: .65rem; font-weight: 700;
  background: #6366f1; color: #fff;
  padding: 2px 8px; border-radius: 10px; letter-spacing: .04em;
}

/* ── Main layout: stocks left, sidebar right ── */
.main-layout {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}
.main-content {
  flex: 1;
  min-width: 0;
}
.sidebar {
  width: 280px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  position: sticky;
  top: 16px;
  max-height: calc(100vh - 32px);
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: #cbd5e1 transparent;
}
.sidebar::-webkit-scrollbar { width: 5px; }
.sidebar::-webkit-scrollbar-track { background: transparent; }
.sidebar::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 3px; }

/* ── Tracking section (sidebar) ── */
.tracking-section {
  background: #fff;
  border: 1px solid #dbeafe;
  border-radius: 8px;
  font-size: .82em;
}
.tracking-header {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 7px 10px;
  background: #eff6ff;
  border-bottom: 1px solid #dbeafe;
  flex-wrap: wrap;
}
.tracking-add-row {
  margin-left: auto;
  display: flex;
  gap: 4px;
  align-items: center;
}
#add-ticker-input {
  font-size: .7rem;
  padding: 3px 6px;
  border: 1px solid #cbd5e1;
  border-radius: 4px;
  width: 80px;
  outline: none;
  color: #1e293b;
  background: #fff;
}
#add-ticker-input:focus { border-color: #3b82f6; box-shadow: 0 0 0 2px #3b82f620; }
#add-ticker-btn {
  font-size: .85rem; line-height: 1;
  padding: 2px 7px;
  background: #3b82f6; color: #fff;
  border: none; border-radius: 4px; cursor: pointer;
  font-weight: 700;
}
#add-ticker-btn:hover { background: #2563eb; }
.tracking-empty {
  font-size: .68rem; color: #94a3b8;
  padding: 10px 12px; text-align: center;
}
.tracking-card {
  border: 1px solid #dbeafe !important;
  background: #f8fbff !important;
  padding: 8px 10px !important;
}
.tracking-avg-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 2px;
}
.tracking-avg-row label {
  font-size: .62rem; color: #64748b;
  display: flex; align-items: center; gap: 4px;
}
.avg-price-input {
  width: 68px; padding: 2px 5px;
  font-size: .65rem; border: 1px solid #cbd5e1;
  border-radius: 3px; outline: none; color: #1e293b; background: #fff;
}
.avg-price-input:focus { border-color: #3b82f6; }
.tracking-remove {
  margin-left: auto;
  font-size: .62rem; color: #94a3b8;
  background: none; border: 1px solid #e2e8f0;
  padding: 1px 5px; border-radius: 3px; cursor: pointer;
}
.tracking-remove:hover { color: #ef4444; border-color: #fca5a5; }

/* ── Section details ── */
details { margin-bottom: 12px; }
summary { list-style: none; cursor: pointer; user-select: none; }
summary::-webkit-details-marker { display: none; }
.section-header {
  display: flex; align-items: center; gap: 6px;
  padding: 6px 2px;
  border-bottom: 1px solid #e2e8f0;
  margin-bottom: 8px;
}
.section-chevron {
  font-size: .55rem; color: #94a3b8;
  transition: transform .15s ease;
  display: inline-block;
  transform: rotate(0deg);
}
details[open] > summary .section-chevron { transform: rotate(90deg); }
.section-icon   { font-size: .75rem; color: #94a3b8; width: 16px; text-align: center; }
.section-title  { font-size: .66rem; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; color: #475569; }
.count          { background: #e2e8f0; color: #64748b; font-size: .58rem; font-weight: 600; padding: 1px 6px; border-radius: 8px; }
.section-sub    { font-size: .63rem; color: #94a3b8; }
.section-body   { display: flex; flex-direction: column; gap: 8px; padding-bottom: 4px; }

/* ── Cards ── */
.card {
  position: relative;
  background: #fff;
  border-radius: 6px;
  padding: 11px 14px;
  box-shadow: 0 1px 3px rgba(0,0,0,.07);
  display: flex; flex-direction: column; gap: 5px;
  transition: box-shadow .15s;
  cursor: pointer;
}
.card:hover { box-shadow: 0 2px 8px rgba(0,0,0,.1); }

.card-header    { display: flex; align-items: center; gap: 6px; }
.card-title-group { display: flex; align-items: baseline; gap: 6px; flex: 1; min-width: 0; }
.ticker         { font-weight: 700; font-size: .9rem; color: #0f172a; white-space: nowrap; }
.company-name   { font-size: .66rem; color: #94a3b8; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stale-badge    { font-size: .57rem; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; background: #f1f5f9; color: #94a3b8; border: 1px solid #e2e8f0; padding: 1px 5px; border-radius: 3px; flex-shrink: 0; }
.action-badge   { font-size: .58rem; font-weight: 600; letter-spacing: .04em; padding: 2px 6px; border-radius: 4px; text-transform: uppercase; white-space: nowrap; flex-shrink: 0; }
.age            { font-size: .66rem; color: #94a3b8; white-space: nowrap; flex-shrink: 0; }
.star-btn {
  background: none; border: none; cursor: pointer;
  font-size: .88rem; color: #cbd5e1;
  padding: 0 1px; line-height: 1;
  transition: color .1s; flex-shrink: 0;
}
.star-btn:hover    { color: #f59e0b; }
.star-btn.star-active { color: #f59e0b; }

/* ── Score row ── */
.score-row       { display: flex; align-items: center; gap: 8px; }
.bar-track       { flex: 1; height: 5px; background: #e2e8f0; border-radius: 3px; overflow: hidden; }
.bar-fill        { height: 100%; border-radius: 3px; }
.score-label     { font-size: .74rem; font-weight: 700; color: #1e293b; min-width: 26px; text-align: right; }
.strength-label  { font-size: .62rem; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; min-width: 58px; }
.trend           { font-size: .88rem; font-weight: 700; min-width: 12px; text-align: center; }

/* ── Verdict ── */
.verdict { font-size: .74rem; color: #475569; line-height: 1.45; }

/* ── NEW tag ── */
.new-tag { font-size: .55rem; font-weight: 700; color: #fff; background: #ef4444; padding: 1px 6px; border-radius: 4px; margin-left: 6px; letter-spacing: .04em; vertical-align: middle; animation: new-pulse 2s ease-in-out infinite; }
@keyframes new-pulse { 0%,100%{opacity:1} 50%{opacity:.6} }

/* ── Signal row ── */
.signal-row  { display: flex; align-items: center; gap: 5px; }
.signal-left { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.signal-right { display: flex; align-items: center; gap: 4px; margin-left: auto; flex-shrink: 0; }
.pill        { font-size: .58rem; font-weight: 500; background: #f8fafc; color: #475569; border: 1px solid #e2e8f0; padding: 2px 7px; border-radius: 10px; white-space: nowrap; }
.evo-wrapper { position: relative; display: inline-flex; margin-left: 6px; cursor: help; }
.evolution-badge { font-size: .65rem; font-weight: 600; padding: 1px 6px; border-radius: 4px; background: rgba(0,0,0,.04); white-space: nowrap; }
.evolution-alert { animation: pulse-alert 2s ease-in-out infinite; }
@keyframes pulse-alert { 0%,100%{box-shadow:none} 50%{box-shadow:0 0 8px rgba(239,68,68,.5)} }
.evo-popup { display: none; position: absolute; bottom: calc(100% + 6px); left: 50%; transform: translateX(-50%); background: #1e293b; color: #f1f5f9; font-size: .62rem; font-weight: 400; padding: 6px 10px; border-radius: 6px; max-width: 260px; width: max-content; white-space: normal; line-height: 1.4; z-index: 50; box-shadow: 0 4px 12px rgba(0,0,0,.25); pointer-events: none; }
.evo-popup::after { content: ""; position: absolute; top: 100%; left: 50%; transform: translateX(-50%); border: 5px solid transparent; border-top-color: #1e293b; }
.evo-wrapper:hover .evo-popup { display: block; }
.action-signal { font-size: .6rem; font-weight: 700; padding: 2px 8px; border-radius: 10px; margin-left: 6px; white-space: nowrap; letter-spacing: .03em; }
.action-signal-strong-buy { background: #14532d; color: #4ade80; border: 1px solid #22c55e; }
.action-signal-buy { background: #dcfce7; color: #16a34a; border: 1px solid #86efac; }
.action-signal-take-profit { background: #fefce8; color: #ca8a04; border: 1px solid #fde047; }
.action-signal-sell { background: #fef2f2; color: #dc2626; border: 1px solid #fca5a5; }
.signal-new { animation: signal-pulse 2s ease-in-out infinite; }
@keyframes signal-pulse { 0%,100% { box-shadow: none; } 50% { box-shadow: 0 0 8px currentColor; } }
.evo-bars { display: flex; gap: 2px; align-items: flex-end; height: 18px; margin-left: 6px; }
.evo-bar { width: 5px; border-radius: 2px 2px 0 0; }
.evo-bar-up { background: #22c55e; }
.evo-bar-stable { background: #cbd5e1; }
.evo-bar-down { background: #ef4444; }

/* ── News card (sidebar) ── */
.news-card { padding: 8px 10px !important; }
.news-subject {
  font-size: .68rem; font-weight: 500; color: #475569;
  flex: 1; line-height: 1.3;
  overflow: hidden; display: -webkit-box;
  -webkit-line-clamp: 2; -webkit-box-orient: vertical;
}

/* ── News ticker pill highlighting ── */
.news-ticker-pill.tracked-bullish {
  background: #22c55e20 !important;
  color: #16a34a !important;
  border-color: #22c55e !important;
  font-weight: 600 !important;
}
.news-ticker-pill.tracked-bearish {
  background: #ef444420 !important;
  color: #dc2626 !important;
  border-color: #ef4444 !important;
  font-weight: 600 !important;
}

/* ── Click popup (replaces hover tooltip) ── */
.popup {
  display: none;
}
#active-popup {
  display: block;
  position: fixed;
  z-index: 99999;
  background: rgb(30, 41, 59);
  color: #cbd5e1;
  border-radius: 8px;
  padding: 14px 16px;
  max-width: 400px;
  min-width: 220px;
  max-height: 70vh;
  overflow-y: auto;
  font-size: .73rem;
  line-height: 1.55;
  box-shadow: 0 10px 36px rgba(0,0,0,.5);
}
.tt-subject  { font-weight: 600; color: #f1f5f9; margin-bottom: 7px; font-size: .74rem; line-height: 1.3; }
.tt-thesis   { color: #94a3b8; margin-bottom: 8px; font-size: .72rem; }
.tt-claims   { display: flex; flex-direction: column; gap: 3px; margin-bottom: 5px; }
.tt-claim    { display: flex; gap: 6px; align-items: flex-start; }
.tt-icon     { font-size: .75rem; flex-shrink: 0; margin-top: 1px; }
.tt-bb       { margin-top: 7px; display: flex; flex-direction: column; gap: 4px; border-top: 1px solid #334155; padding-top: 7px; }
.tt-bull     { color: #4ade80; font-size: .71rem; }
.tt-bear     { color: #f87171; font-size: .71rem; }

/* ── News popup ticker breakdown ── */
.news-popup-ticker {
  border-top: 1px solid #334155;
  padding-top: 8px;
  margin-top: 8px;
}
.news-popup-ticker:first-of-type { border-top: none; margin-top: 4px; }
.news-popup-ticker-header {
  font-weight: 700;
  color: #e2e8f0;
  font-size: .76rem;
  margin-bottom: 4px;
  letter-spacing: .02em;
  display: flex;
  align-items: center;
  gap: 8px;
}
.sentiment-pill {
  font-size: .62rem;
  font-weight: 600;
  padding: 1px 7px;
  border: 1px solid;
  border-radius: 10px;
  white-space: nowrap;
  letter-spacing: .02em;
}

/* ── Sidebar news section ── */
.sidebar .section-body { gap: 6px; }
.sidebar details { margin-bottom: 8px; }

/* ── Responsive: stack on small screens ── */
@media (max-width: 800px) {
  .main-layout { flex-direction: column; }
  .sidebar { width: 100%; position: static; }
}
"""


# ── TRACKING HTML (static shell) ──────────────────────────────────────────────

_TRACKING_HTML = """<section class="tracking-section" id="tracking-section">
  <div class="tracking-header">
    <span class="section-icon">📍</span>
    <span class="section-title" style="font-size:.66rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#475569">TRACKING</span>
    <span class="section-sub" style="font-size:.63rem;color:#94a3b8">Positions you hold</span>
    <div class="tracking-add-row">
      <input id="add-ticker-input" type="text" placeholder="Add ticker…" autocomplete="off" spellcheck="false">
      <button id="add-ticker-btn">+</button>
    </div>
  </div>
  <div id="tracking-cards" class="section-body" style="padding:8px 14px 4px"></div>
  <p id="tracking-empty" class="tracking-empty">
    No stocks tracked yet — click ☆ on any card below, or type a ticker above.
  </p>
</section>"""


# ── JAVASCRIPT ────────────────────────────────────────────────────────────────

_JS = r"""
// ── Click Popup ──────────────────────────────────────────────────────────────
var floatingPopup = null;

function closePopup() {
  if (floatingPopup) {
    floatingPopup.remove();
    floatingPopup = null;
  }
}

function openPopup(card) {
  var src = card.querySelector('.popup');
  if (!src) return;
  closePopup();

  // Clone popup content and attach to body (escapes any stacking context / opacity)
  floatingPopup = src.cloneNode(true);
  floatingPopup.removeAttribute('class');
  floatingPopup.id = 'active-popup';
  document.body.appendChild(floatingPopup);

  // Position near the card
  var rect = card.getBoundingClientRect();
  var pad = 10;
  var pw = floatingPopup.offsetWidth || 400;
  var ph = floatingPopup.offsetHeight || 200;

  var x = rect.right + pad;
  if (x + pw > window.innerWidth - 12) {
    x = rect.left - pw - pad;
  }
  if (x < 8) x = 8;

  var y = rect.top;
  if (y + ph > window.innerHeight - 12) {
    y = window.innerHeight - ph - 12;
  }
  if (y < 8) y = 8;

  floatingPopup.style.left = x + 'px';
  floatingPopup.style.top = y + 'px';
}

document.querySelectorAll('.main-content .card, .sidebar .news-card').forEach(function(card) {
  card.addEventListener('click', function(e) {
    if (e.target.closest('.star-btn')) return;
    e.stopPropagation();
    openPopup(card);
  });
});

// Close popup when clicking anywhere outside the popup
document.addEventListener('click', function(e) {
  if (!floatingPopup) return;
  if (floatingPopup.contains(e.target)) return;
  closePopup();
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closePopup();
});

// ── Tracking ─────────────────────────────────────────────────────────────────
var SC_MAP = {STRONG:'#22c55e', MODERATE:'#f59e0b', WEAK:'#ef4444', UNCLEAR:'#94a3b8'};
var AC_MAP = {RESEARCH_FURTHER:'#6366f1', MONITOR:'#3b82f6', WAIT_FOR_DIPS:'#f59e0b', IGNORE:'#94a3b8'};
var AL_MAP = {RESEARCH_FURTHER:'RESEARCH', MONITOR:'MONITOR', WAIT_FOR_DIPS:'WAIT FOR DIP', IGNORE:'IGNORE'};
var TC_MAP = {'\u2191':'#22c55e', '\u2193':'#ef4444', '\u2192':'#94a3b8'};

function getTracking() {
  try { return JSON.parse(localStorage.getItem('tracking') || '[]'); }
  catch(e) { return []; }
}
function saveTracking(list) {
  localStorage.setItem('tracking', JSON.stringify(list));
}

function renderTracking() {
  var list      = getTracking();
  var container = document.getElementById('tracking-cards');
  var empty     = document.getElementById('tracking-empty');

  // Sync star buttons across all cards
  document.querySelectorAll('.star-btn').forEach(function(btn) {
    var tracked = list.some(function(x) { return x.ticker === btn.dataset.ticker; });
    btn.textContent = tracked ? '\u2605' : '\u2606';
    btn.title       = tracked ? 'Remove from tracking' : 'Track this stock';
    btn.classList.toggle('star-active', tracked);
  });

  if (list.length === 0) {
    container.innerHTML  = '';
    empty.style.display  = 'block';
    highlightNewsPills();
    return;
  }
  empty.style.display = 'none';

  container.innerHTML = list.map(function(entry) {
    var data = STOCK_DATA[entry.ticker];
    if (!data) {
      return '<div class="card tracking-card">'
        + '<div class="card-header">'
        + '<span class="ticker">' + entry.ticker + '</span>'
        + '<span class="age" style="margin-left:auto;color:#ef4444;font-size:.68rem">not in system</span>'
        + '<button class="tracking-remove" data-ticker="' + entry.ticker + '" title="Remove">✕</button>'
        + '</div></div>';
    }

    var sc      = data.score;
    var barPct  = Math.min(100, sc * 10);
    var barC    = SC_MAP[data.strength] || '#94a3b8';
    var ac      = AC_MAP[data.action]   || '#94a3b8';
    var al      = AL_MAP[data.action]   || data.action;
    var tc      = TC_MAP[data.trend]    || '#94a3b8';
    var ap      = (entry.avgPrice !== null && entry.avgPrice !== undefined) ? entry.avgPrice : '';
    var cname   = data.company_name
      ? '<span class="company-name">' + data.company_name + '</span>' : '';

    return '<div class="card tracking-card" data-ticker="' + entry.ticker + '">'
      + '<div class="card-header">'
      + '<div class="card-title-group"><span class="ticker">' + entry.ticker + '</span>' + cname + '</div>'
      + '<span class="action-badge" style="background:' + ac + '18;color:' + ac + ';border:1px solid ' + ac + '44;font-size:.58rem;font-weight:600;padding:2px 6px;border-radius:4px;text-transform:uppercase;letter-spacing:.04em">' + al + '</span>'
      + '<span class="age">' + data.age_label + '</span>'
      + '<button class="tracking-remove" data-ticker="' + entry.ticker + '" title="Remove from tracking">✕</button>'
      + '</div>'
      + '<div class="score-row">'
      + '<div class="bar-track"><div class="bar-fill" style="width:' + barPct + '%;background:' + barC + '"></div></div>'
      + '<span class="score-label">' + sc.toFixed(1) + '</span>'
      + '<span class="strength-label" style="color:' + barC + ';font-size:.62rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em;min-width:58px">' + data.strength + '</span>'
      + '<span class="trend" style="color:' + tc + ';font-size:.88rem;font-weight:700">' + data.trend + '</span>'
      + '</div>'
      + '<p class="verdict">' + data.verdict + '</p>'
      + '<div class="tracking-avg-row">'
      + '<label>Avg entry: <input type="number" class="avg-price-input" value="' + ap
      + '" placeholder="e.g. 14.50" data-ticker="' + entry.ticker + '" step="0.01"></label>'
      + '</div>'
      + '</div>';
  }).join('');

  // Attach events
  container.querySelectorAll('.tracking-remove').forEach(function(btn) {
    btn.addEventListener('click', function() { removeTracking(btn.dataset.ticker); });
  });
  container.querySelectorAll('.avg-price-input').forEach(function(inp) {
    inp.addEventListener('change', function() { updateAvgPrice(inp.dataset.ticker, inp.value); });
  });

  // Update news pill highlighting whenever tracking changes
  highlightNewsPills();
}

function toggleTracking(ticker) {
  var list = getTracking();
  var idx  = list.findIndex(function(x) { return x.ticker === ticker; });
  if (idx >= 0) { list.splice(idx, 1); }
  else          { list.push({ ticker: ticker, avgPrice: null }); }
  saveTracking(list);
  renderTracking();
}

function removeTracking(ticker) {
  saveTracking(getTracking().filter(function(x) { return x.ticker !== ticker; }));
  renderTracking();
}

function updateAvgPrice(ticker, val) {
  var list  = getTracking();
  var entry = list.find(function(x) { return x.ticker === ticker; });
  if (entry) {
    entry.avgPrice = val ? parseFloat(val) : null;
    saveTracking(list);
  }
}

function addTickerFromInput() {
  var input  = document.getElementById('add-ticker-input');
  var ticker = input.value.trim().toUpperCase();
  if (!ticker) return;
  var list = getTracking();
  if (!list.some(function(x) { return x.ticker === ticker; })) {
    list.push({ ticker: ticker, avgPrice: null });
    saveTracking(list);
    renderTracking();
  }
  input.value = '';
  input.focus();
}

// ── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
  // Star buttons
  document.querySelectorAll('.star-btn').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      toggleTracking(btn.dataset.ticker);
    });
  });

  // Enter key on add-ticker input
  var addInput = document.getElementById('add-ticker-input');
  if (addInput) {
    addInput.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') addTickerFromInput();
    });
  }
  var addBtn = document.getElementById('add-ticker-btn');
  if (addBtn) {
    addBtn.addEventListener('click', addTickerFromInput);
  }

  // Initial tracking render
  renderTracking();
});

// ── News ticker pill highlighting ────────────────────────────────────────────
function highlightNewsPills() {
  var tracked = getTracking();
  var trackedTickers = {};
  tracked.forEach(function(entry) { trackedTickers[entry.ticker] = true; });

  document.querySelectorAll('.news-ticker-pill').forEach(function(pill) {
    var ticker = pill.dataset.ticker;
    pill.classList.remove('tracked-bullish', 'tracked-bearish');
    if (!trackedTickers[ticker]) return;

    var data = STOCK_DATA[ticker];
    if (!data) return;

    // Determine bullish/bearish from strength
    var strength = (data.strength || '').toUpperCase();
    if (strength === 'STRONG' || strength === 'MODERATE') {
      pill.classList.add('tracked-bullish');
    } else {
      pill.classList.add('tracked-bearish');
    }
  });
}

// ── Search / Filter ──────────────────────────────────────────────────────────
(function() {
  var input = document.getElementById('search-filter');
  if (!input) return;
  input.addEventListener('input', function() {
    var q = input.value.trim().toLowerCase();
    document.querySelectorAll('.main-content .section-details').forEach(function(section) {
      var cards = section.querySelectorAll('.card');
      var visible = 0;
      cards.forEach(function(card) {
        var ticker = (card.dataset.ticker || '').toLowerCase();
        var cname  = (card.querySelector('.company-name') || {}).textContent || '';
        var match  = !q || ticker.indexOf(q) >= 0 || cname.toLowerCase().indexOf(q) >= 0;
        card.style.display = match ? '' : 'none';
        if (match) visible++;
      });
      section.style.display = (q && visible === 0) ? 'none' : '';
    });
  });
})();
"""


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ticker_data, news_emails = load_signals_from_analyses()
    rows = build_rows(ticker_data)
    html = build_html(rows, news_emails)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Dashboard written to {OUTPUT_FILE} ({len(rows)} stocks, {len(news_emails)} news emails)")
