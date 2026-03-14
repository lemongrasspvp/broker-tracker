"""
analyzer.py
Orchestrates the full analysis pipeline:
1. Extract stock tickers from email thread
2. Enrich each ticker with financial data
3. Load per-ticker memory context
4. Send everything to Claude with web search + images enabled
5. Return structured per-stock analyses
"""

import re
import json
import anthropic
from datetime import datetime

from enricher import fetch_stock_data, fetch_macro_context, format_stock_data_for_prompt, guess_country
from memory import format_history_for_prompt, store_analysis
from broker_tracker import log_recommendation, get_ticker_track_record


# ── PROMPTS ────────────────────────────────────────────────────────────────────

TICKER_EXTRACTION_PROMPT = """You are reading a broker email. Extract every stock or asset mentioned.

For each one return:
- ticker: the stock ticker symbol (add exchange suffix if you know it: .OL for Norway, .ST for Sweden, .CO for Denmark, .HE for Finland, nothing for US)
- company_name: full company name
- country: two-letter country code (NO, SE, DK, FI, US, or INTL)
- is_primary: true if this is a main recommendation, false if just mentioned in passing

Respond ONLY with a JSON array, no markdown, no explanation:
[{{"ticker": "EQNR.OL", "company_name": "Equinor", "country": "NO", "is_primary": true}}]

EMAIL:
{email_text}"""


ANALYSIS_PROMPT = """You are a senior investment analyst reviewing research from {broker_name}.
This broker specializes in Nordic energy/shipping/offshore. Stocks outside this universe need extra verification via web search.

Use web search to verify key claims, check recent news, and sample sentiment on: {forums}

EMAIL THREAD ({thread_length} email(s), oldest first):
{email_content}

FINANCIAL DATA:
{financial_data}

PREVIOUS ANALYSES:
{memory_context}

BROKER TRACK RECORD:
{track_record}

Analyze each stock separately. You MUST respond with ONLY a JSON object (no commentary, no markdown fences). Start your response with {{ and end with }}:

{{
  "stocks": [
    {{
      "ticker": "string",
      "company_name": "string",
      "country": "NO|SE|DK|FI|US|INTL",
      "is_core_coverage": true,
      "investment_thesis": "2-3 sentences",
      "thesis_strength": "STRONG|MODERATE|WEAK|UNCLEAR",
      "thesis_strength_score": 0,
      "key_claims": [{{"claim": "...", "assessment": "CREDIBLE|QUESTIONABLE|FALSE|UNVERIFIABLE", "verified_via": "...", "note": "..."}}],
      "bull_case": "string",
      "bear_case": "string",
      "broker_flags": ["string"],
      "forum_sentiment": "one sentence on market sentiment",
      "insider_read": "one sentence on insider activity",
      "short_interest_read": "one sentence on short interest",
      "catalyst_note": "string",
      "vs_consensus": "string",
      "recommended_action": "RESEARCH_FURTHER|MONITOR|WAIT_FOR_DIPS|IGNORE",
      "one_line_verdict": "string"
    }}
  ],
  "thread_narrative": "string",
  "thesis_evolution": "STRENGTHENING|STABLE|WEAKENING|CONTRADICTORY|N/A",
  "urgency_escalation": false,
  "overall_email_quality": "HIGH|MEDIUM|LOW",
  "overall_email_quality_note": "string"
}}"""


# ── HELPERS ────────────────────────────────────────────────────────────────────

def format_thread_for_prompt(thread: list[dict]) -> str:
    blocks = []
    for i, em in enumerate(thread, 1):
        blocks.append(
            f"--- EMAIL {i} of {len(thread)} ---\n"
            f"Date:    {em['date']}\n"
            f"Subject: {em['subject']}\n\n"
            f"{em['body']}"
        )
    return "\n\n".join(blocks)


def extract_all_images(thread: list[dict]) -> list[dict]:
    """Collect all images from all emails in a thread (max 6 total)."""
    images = []
    for em in thread:
        for img in em.get("images", []):
            if len(images) >= 3:
                break
            images.append(img)
    return images


def extract_tickers(client: anthropic.Anthropic, thread: list[dict]) -> list[dict]:
    """Ask Claude to identify stock tickers mentioned in the email thread."""
    # Combine subject lines and first 2000 chars of each email for ticker extraction
    combined = "\n".join([
        f"Subject: {em['subject']}\n{em['body'][:1500]}"
        for em in thread
    ])

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{
                "role":    "user",
                "content": TICKER_EXTRACTION_PROMPT.format(email_text=combined[:4000])
            }]
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        tickers = json.loads(raw)
        return tickers if isinstance(tickers, list) else []
    except Exception as e:
        print(f"  [WARN] Ticker extraction failed: {e}")
        return []


def build_content_blocks(email_content: str, images: list[dict]) -> list[dict]:
    """Build the content array for the Claude API call, including images."""
    blocks = [{"type": "text", "text": email_content}]
    for img in images:
        blocks.append({
            "type":   "image",
            "source": {
                "type":       "base64",
                "media_type": img["media_type"],
                "data":       img["data"],
            }
        })
    return blocks


# ── MAIN ANALYSIS ──────────────────────────────────────────────────────────────

def _match_stock_data(ticker: str, stock_data_map: dict) -> dict:
    """Fuzzy-match Claude's output ticker against the stock_data_map keys.

    Claude sometimes returns compound tickers like "NVO / NOVO.B" or
    "CSI300/HSCEI" while the map is keyed by the extraction ticker "NVO".
    """
    if ticker in stock_data_map:
        return stock_data_map[ticker]

    # Try the first part of a compound ticker  ("NVO / NOVO.B" → "NVO")
    for sep in (" / ", "/"):
        if sep in ticker:
            first = ticker.split(sep)[0].strip()
            if first in stock_data_map:
                return stock_data_map[first]

    # Try matching any map key that is a prefix of the ticker or vice-versa
    ticker_base = ticker.split("/")[0].split(" ")[0].strip().upper()
    for key in stock_data_map:
        key_base = key.split("/")[0].split(" ")[0].strip().upper()
        if ticker_base == key_base:
            return stock_data_map[key]

    return {}


def analyze_thread(thread: list[dict], broker_name: str, api_key: str) -> dict | None:
    """
    Full analysis pipeline for a thread of emails.
    Returns the complete analysis dict.
    """
    client = anthropic.Anthropic(api_key=api_key)

    # 1. Extract tickers
    print(f"  → Identifying stocks...")
    tickers = extract_tickers(client, thread)
    primary = [t for t in tickers if t.get("is_primary")]
    if not primary:
        primary = tickers  # fallback: use all if none flagged primary

    if not primary:
        print(f"  [WARN] No stocks identified in this email thread, skipping.")
        return None

    # Filter out entries with missing tickers
    primary = [t for t in primary if t.get("ticker")]
    if not primary:
        print(f"  [WARN] No valid tickers found, skipping.")
        return None
    print(f"  → Found: {', '.join(t['ticker'] for t in primary)}")

    # 2. Enrich each ticker with financial data
    financial_blocks = []
    macro_done = set()
    stock_data_map = {}

    for t in primary:
        country = t.get("country", guess_country(t["ticker"], t.get("company_name", "")))
        print(f"  → Fetching data for {t['ticker']}...")
        data = fetch_stock_data(t["ticker"], country)
        stock_data_map[t["ticker"]] = data
        financial_blocks.append(format_stock_data_for_prompt(data))

        if country not in macro_done:
            macro = fetch_macro_context(country)
            if macro:
                macro_str = "  Macro: " + " | ".join(f"{k}: {v}" for k, v in macro.items())
                financial_blocks.append(macro_str)
            macro_done.add(country)

    financial_data = "\n\n".join(financial_blocks)

    # 3. Load memory + track record for each ticker
    memory_blocks = []
    track_blocks  = []
    for t in primary:
        memory_blocks.append(format_history_for_prompt(t["ticker"]))
        track_blocks.append(get_ticker_track_record(t["ticker"]))

    # 4. Determine forums based on countries present
    countries = list({t.get("country", "NO") for t in primary})
    from enricher import COUNTRY_CONFIG
    forums = []
    for c in countries:
        forums += COUNTRY_CONFIG.get(c, COUNTRY_CONFIG["INTL"])["forums"]
    forums_str = ", ".join(set(forums))

    # 5. Build prompt
    email_content   = format_thread_for_prompt(thread)
    images          = extract_all_images(thread)
    content_text    = ANALYSIS_PROMPT.format(
        broker_name    = broker_name,
        thread_length  = len(thread),
        email_content  = email_content,
        financial_data = financial_data,
        memory_context = "\n\n".join(memory_blocks),
        track_record   = "\n\n".join(track_blocks),
        forums         = forums_str,
    )

    content_blocks = build_content_blocks(content_text, images)
    if images:
        print(f"  → Including {len(images)} image(s) from email")

    # 6. Call Claude with web search enabled (retry on rate limit)
    print(f"  → Running analysis with web search...")
    import time as _time
    response = None
    for attempt in range(4):
        try:
            response = client.messages.create(
                model      = "claude-sonnet-4-6",
                max_tokens = 16000,
                tools      = [{"type": "web_search_20250305", "name": "web_search"}],
                messages   = [{"role": "user", "content": content_blocks}]
            )
            break
        except Exception as e:
            msg = str(e)
            if "429" in msg or "rate_limit" in msg:
                wait = 30 * (2 ** attempt)
                print(f"  [RATE LIMIT] Waiting {wait}s before retry {attempt + 1}/3...")
                _time.sleep(wait)
            else:
                print(f"  [ERROR] Claude API failed: {e}")
                return None

    if response is None:
        print(f"  [ERROR] Claude API failed after retries (rate limit)")
        return None

    # Check if output was truncated
    if response.stop_reason == "max_tokens":
        print(f"  [WARN] Response truncated (max_tokens hit)")

    # Collect all text blocks and find the one with JSON
    text_blocks = [block.text for block in response.content if hasattr(block, "text")]
    analysis = None

    for raw in reversed(text_blocks):  # try last block first
        raw = raw.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        # Find JSON object within the text
        json_match = re.search(r"\{[\s\S]*\}", raw)
        if json_match:
            try:
                analysis = json.loads(json_match.group(0))
                break
            except json.JSONDecodeError:
                continue

    if analysis is None:
        combined = " | ".join(f"[{len(t)} chars]" for t in text_blocks)
        print(f"  [ERROR] No valid JSON found in {len(text_blocks)} text block(s): {combined}")
        if text_blocks:
            last = text_blocks[-1]
            print(f"  [DEBUG] Last block tail: ...{last[-200:]}")
        return None

    # 7. Attach metadata and store to memory/tracker
    latest = thread[-1]
    analysis["_email_subject"]  = latest["subject"]
    analysis["_email_date"]     = latest["date"]
    analysis["_thread_length"]  = len(thread)
    analysis["_analyzed_at"]    = datetime.now().isoformat()
    analysis["_image_count"]    = len(images)

    for stock_analysis in analysis.get("stocks", []):
        ticker = stock_analysis.get("ticker", "")
        data   = _match_stock_data(ticker, stock_data_map)
        price  = data.get("current_price")
        stock_analysis["_price_at_analysis"] = price

        store_analysis(ticker, stock_analysis)
        if stock_analysis.get("recommended_action") != "IGNORE":
            log_recommendation(
                ticker         = ticker,
                action         = stock_analysis.get("recommended_action", ""),
                thesis_strength= stock_analysis.get("thesis_strength", ""),
                score          = stock_analysis.get("thesis_strength_score", 0),
                verdict        = stock_analysis.get("one_line_verdict", ""),
                current_price  = price,
            )

    return analysis
