"""
enricher.py
Pulls financial data for each identified stock ticker using yfinance.
Handles Norwegian (.OL), Swedish (.ST), Danish (.CO), Finnish (.HE) suffixes.
Also pulls relevant macro context (Brent crude, NOK/USD).
"""

import yfinance as yf
from datetime import datetime, timedelta
import re

# Country → exchange suffix + relevant forums
COUNTRY_CONFIG = {
    "NO": {
        "suffix":     ".OL",
        "forums":     ["hegnar.no/forum", "shareville.no", "site:reddit.com/r/Norway finance"],
        "macro_note": "Oslo Børs — check Brent crude (BZ=F) and NOK/USD (USDNOK=X)",
    },
    "SE": {
        "suffix":     ".ST",
        "forums":     ["site:borsforumse.se", "site:shareville.com/sv", "site:reddit.com/r/sweden finance", "site:flashback.org aktier"],
        "macro_note": "Nasdaq Stockholm — check SEK/USD (USDSEK=X)",
    },
    "DK": {
        "suffix":     ".CO",
        "forums":     ["site:euroinvestor.dk/forum", "site:reddit.com/r/Denmark finance"],
        "macro_note": "Nasdaq Copenhagen — check DKK/USD (USDDKK=X)",
    },
    "FI": {
        "suffix":     ".HE",
        "forums":     ["site:kauppalehti.fi", "site:reddit.com/r/Finland finance"],
        "macro_note": "Nasdaq Helsinki — check EUR/USD (EURUSD=X)",
    },
    "US": {
        "suffix":     "",
        "forums":     ["site:reddit.com/r/stocks", "site:reddit.com/r/investing", "site:seekingalpha.com"],
        "macro_note": "NYSE/NASDAQ — check S&P 500 (^GSPC) and USD index (DX-Y.NYB)",
    },
    "INTL": {
        "suffix":     "",
        "forums":     ["site:reddit.com/r/investing", "site:seekingalpha.com"],
        "macro_note": "International — verify exchange and relevant macro",
    },
}

MACRO_TICKERS = {
    "brent_crude": "BZ=F",
    "usd_nok":     "USDNOK=X",
    "usd_sek":     "USDSEK=X",
    "eur_usd":     "EURUSD=X",
}


def guess_country(ticker: str, company_hint: str = "") -> str:
    """Guess country from ticker suffix or company name hint."""
    ticker_upper = ticker.upper()
    if ticker_upper.endswith(".OL"):   return "NO"
    if ticker_upper.endswith(".ST"):   return "SE"
    if ticker_upper.endswith(".CO"):   return "DK"
    if ticker_upper.endswith(".HE"):   return "FI"
    # If no suffix, check hints
    hint = company_hint.lower()
    if any(w in hint for w in ["norsk", "norwegian", "oslo", "equinor", "statoil", "yara"]):
        return "NO"
    if any(w in hint for w in ["swedish", "sweden", "stockholm", "volvo", "ericsson", "h&m"]):
        return "SE"
    # Default international for unsuffixed tickers
    if re.match(r'^[A-Z]{1,5}$', ticker_upper):
        return "US"
    return "INTL"


def resolve_ticker(raw_ticker: str, country: str) -> str:
    """Normalise ticker to the correct Yahoo Finance format for the given country."""
    t = raw_ticker.upper().strip()

    # Handle compound tickers like "NVO / NOVO.B" or "CSI300/HSCEI" — use first part
    for sep in (" / ", "/"):
        if sep in t:
            t = t.split(sep)[0].strip()
            break

    # Remove exchange prefixes like "NASDAQ:" or "HKG:"
    t = re.sub(r"^[A-Z0-9]+:", "", t)

    # Strip all known and invalid suffixes so we start from a clean base symbol
    _all_suffixes = [".OL", ".ST", ".SS", ".CO", ".HE", ".US", ".LON", ".NYSE", ".INTL", ".HK"]
    changed = True
    while changed:
        changed = False
        for s in _all_suffixes:
            if t.endswith(s):
                t = t[:-len(s)]
                changed = True
                break

    # Re-apply the correct suffix for the target country
    suffix = COUNTRY_CONFIG.get(country, {}).get("suffix", "")
    return t + suffix


def safe_get(d: dict, *keys, default=None):
    """Safely navigate nested dict."""
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
    return d


def fetch_stock_data(raw_ticker: str, country: str = None) -> dict:
    """
    Fetch key financial data for a stock.
    Returns a dict with price, valuation, consensus, insider, short interest, earnings.
    """
    if country is None:
        country = guess_country(raw_ticker)

    ticker_str = resolve_ticker(raw_ticker, country)
    result = {
        "ticker":           ticker_str,
        "country":          country,
        "forums":           COUNTRY_CONFIG.get(country, COUNTRY_CONFIG["INTL"])["forums"],
        "macro_note":       COUNTRY_CONFIG.get(country, COUNTRY_CONFIG["INTL"])["macro_note"],
        "error":            None,
        "name":             None,
        "current_price":    None,
        "currency":         None,
        "market_cap":       None,
        "pe_ratio":         None,
        "forward_pe":       None,
        "ev_ebitda":        None,
        "price_to_book":    None,
        "analyst_target":   None,
        "analyst_low":      None,
        "analyst_high":     None,
        "analyst_count":    None,
        "recommendation":   None,
        "short_percent":    None,
        "insider_buy_sell": None,
        "recent_insiders":  [],
        "next_earnings":    None,
        "52w_high":         None,
        "52w_low":          None,
        "ytd_return":       None,
        "sector":           None,
        "industry":         None,
    }

    try:
        stock = yf.Ticker(ticker_str)
        info  = stock.info or {}

        result["name"]          = info.get("longName") or info.get("shortName")
        result["current_price"] = info.get("currentPrice") or info.get("regularMarketPrice")
        result["currency"]      = info.get("currency")
        result["market_cap"]    = info.get("marketCap")
        result["pe_ratio"]      = info.get("trailingPE")
        result["forward_pe"]    = info.get("forwardPE")
        result["ev_ebitda"]     = info.get("enterpriseToEbitda")
        result["price_to_book"] = info.get("priceToBook")
        result["sector"]        = info.get("sector")
        result["industry"]      = info.get("industry")
        result["52w_high"]      = info.get("fiftyTwoWeekHigh")
        result["52w_low"]       = info.get("fiftyTwoWeekLow")

        # Analyst consensus
        result["analyst_target"] = info.get("targetMeanPrice")
        result["analyst_low"]    = info.get("targetLowPrice")
        result["analyst_high"]   = info.get("targetHighPrice")
        result["analyst_count"]  = info.get("numberOfAnalystOpinions")
        result["recommendation"] = info.get("recommendationKey")  # e.g. "buy", "hold"

        # Short interest
        short_pct = info.get("shortPercentOfFloat")
        if short_pct:
            result["short_percent"] = round(short_pct * 100, 1)

        # Insider transactions (last 5)
        try:
            insiders = stock.insider_transactions
            if insiders is not None and not insiders.empty:
                recent = insiders.head(5)
                insider_list = []
                buys, sells = 0, 0
                for _, row in recent.iterrows():
                    txn = {
                        "name":   str(row.get("Insider", "")),
                        "shares": int(row.get("Shares", 0)),
                        "value":  int(row.get("Value", 0)) if row.get("Value") else None,
                        "type":   str(row.get("Transaction", "")),
                        "date":   str(row.get("Start Date", "")),
                    }
                    insider_list.append(txn)
                    t = txn["type"].lower()
                    if "buy" in t or "purchase" in t:
                        buys += 1
                    elif "sell" in t or "sale" in t:
                        sells += 1
                result["recent_insiders"]  = insider_list
                result["insider_buy_sell"] = f"{buys} buys / {sells} sells (last 5 transactions)"
        except Exception:
            pass

        # Next earnings date
        try:
            cal = stock.calendar
            if cal is not None:
                if isinstance(cal, dict):
                    ed = cal.get("Earnings Date")
                    if ed:
                        result["next_earnings"] = str(ed[0]) if isinstance(ed, list) else str(ed)
                elif hasattr(cal, "columns") and "Earnings Date" in cal.columns:
                    result["next_earnings"] = str(cal["Earnings Date"].iloc[0])
        except Exception:
            pass

        # YTD return
        try:
            hist = stock.history(period="ytd")
            if not hist.empty and len(hist) > 1:
                ytd_start = hist["Close"].iloc[0]
                ytd_now   = hist["Close"].iloc[-1]
                result["ytd_return"] = round(((ytd_now - ytd_start) / ytd_start) * 100, 1)
        except Exception:
            pass

    except Exception as e:
        result["error"] = str(e)

    return result


def fetch_macro_context(country: str) -> dict:
    """Fetch relevant macro data based on country."""
    macro = {}
    tickers_to_fetch = []

    if country == "NO":
        tickers_to_fetch = [("brent_crude", "BZ=F"), ("usd_nok", "USDNOK=X")]
    elif country == "SE":
        tickers_to_fetch = [("usd_sek", "USDSEK=X")]
    elif country in ("DK", "FI"):
        tickers_to_fetch = [("eur_usd", "EURUSD=X")]
    elif country == "US":
        tickers_to_fetch = [("eur_usd", "EURUSD=X")]

    for label, sym in tickers_to_fetch:
        try:
            t = yf.Ticker(sym)
            price = t.info.get("regularMarketPrice") or t.info.get("previousClose")
            if price:
                macro[label] = round(price, 2)
        except Exception:
            pass

    return macro


def format_stock_data_for_prompt(data: dict) -> str:
    """Format enriched stock data as a readable block for the analysis prompt."""
    lines = [f"FINANCIAL DATA: {data['ticker']} ({data.get('name', 'N/A')})"]

    if data.get("error"):
        lines.append(f"  [Could not fetch data: {data['error']}]")
        return "\n".join(lines)

    if data.get("current_price"):
        price_line = f"  Price: {data['current_price']} {data.get('currency','')}"
        if data.get("52w_low") and data.get("52w_high"):
            price_line += f"  |  52w range: {data['52w_low']}–{data['52w_high']}"
        if data.get("ytd_return") is not None:
            price_line += f"  |  YTD: {data['ytd_return']:+.1f}%"
        lines.append(price_line)

    valuation_parts = []
    if data.get("pe_ratio"):      valuation_parts.append(f"P/E: {data['pe_ratio']:.1f}")
    if data.get("forward_pe"):    valuation_parts.append(f"Fwd P/E: {data['forward_pe']:.1f}")
    if data.get("ev_ebitda"):     valuation_parts.append(f"EV/EBITDA: {data['ev_ebitda']:.1f}")
    if data.get("price_to_book"): valuation_parts.append(f"P/B: {data['price_to_book']:.1f}")
    if valuation_parts:
        lines.append(f"  Valuation: {' | '.join(valuation_parts)}")

    if data.get("analyst_target"):
        consensus_line = f"  Analyst consensus: target {data['analyst_target']} {data.get('currency','')}"
        if data.get("analyst_low") and data.get("analyst_high"):
            consensus_line += f" (range {data['analyst_low']}–{data['analyst_high']})"
        if data.get("analyst_count"):
            consensus_line += f" | {data['analyst_count']} analysts"
        if data.get("recommendation"):
            consensus_line += f" | consensus: {data['recommendation'].upper()}"
        lines.append(consensus_line)

    if data.get("short_percent") is not None:
        lines.append(f"  Short interest: {data['short_percent']}% of float")

    if data.get("insider_buy_sell"):
        lines.append(f"  Insider activity: {data['insider_buy_sell']}")
        for txn in data.get("recent_insiders", [])[:3]:
            lines.append(f"    • {txn['date']}  {txn['name']}  {txn['type']}  {txn['shares']:,} shares")

    if data.get("next_earnings"):
        lines.append(f"  Next earnings: {data['next_earnings']}")

    if data.get("sector"):
        lines.append(f"  Sector/Industry: {data['sector']} / {data.get('industry','')}")

    lines.append(f"  Macro context: {data.get('macro_note','')}")

    return "\n".join(lines)
