"""
reporter.py
Formats analysis results into human-readable text reports and saves them.
Optionally emails the report.
"""

import json
import os
import re
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


def format_stock_block(s: dict) -> list[str]:
    """Format a single stock analysis into readable lines."""
    strength_emoji  = {"STRONG": "💪", "MODERATE": "🤔", "WEAK": "⚠️", "UNCLEAR": "❓"}.get(s.get("thesis_strength", ""), "")
    action_emoji    = {"RESEARCH_FURTHER": "🔍", "MONITOR": "👀", "IGNORE": "🚫", "WAIT_FOR_DIPS": "⏳"}.get(s.get("recommended_action", ""), "")
    coverage_note   = "" if s.get("is_core_coverage") else "  ⚠️ OUTSIDE CORE COVERAGE — verify independently"
    price_note      = f"  Price at analysis: {s['_price_at_analysis']}" if s.get("_price_at_analysis") else ""

    lines = [
        f"  ┌─ {s.get('ticker','')}  {s.get('company_name','')}  [{s.get('country','')}]",
        f"  │  Strength:  {strength_emoji} {s.get('thesis_strength','')}  ({s.get('thesis_strength_score','?')}/10)",
        f"  │  Action:    {action_emoji} {s.get('recommended_action','')}",
    ]
    if price_note:      lines.append(f"  │  {price_note}")
    if coverage_note:   lines.append(f"  │ {coverage_note}")
    lines.append(f"  │")
    lines.append(f"  │  THESIS:")
    lines.append(f"  │  {s.get('investment_thesis','')}")
    lines.append(f"  │")

    if s.get("key_claims"):
        lines.append(f"  │  KEY CLAIMS:")
        for claim in s["key_claims"]:
            icon = {"CREDIBLE":"✅","QUESTIONABLE":"⚠️","FALSE":"❌","UNVERIFIABLE":"❓"}.get(claim.get("assessment",""),"•")
            lines.append(f"  │  {icon} {claim.get('claim','')}  [{claim.get('verified_via','')}]")
            if claim.get("note"):
                lines.append(f"  │     → {claim['note']}")
        lines.append(f"  │")

    if s.get("forum_sentiment"):
        lines += [f"  │  🌐 MARKET SENTIMENT:", f"  │  {s['forum_sentiment']}", "  │"]

    if s.get("vs_consensus"):
        lines += [f"  │  📊 VS ANALYST CONSENSUS:", f"  │  {s['vs_consensus']}", "  │"]

    if s.get("insider_read"):
        lines += [f"  │  👤 INSIDER ACTIVITY:", f"  │  {s['insider_read']}", "  │"]

    if s.get("short_interest_read"):
        lines += [f"  │  📉 SHORT INTEREST:", f"  │  {s['short_interest_read']}", "  │"]

    if s.get("catalyst_note"):
        lines += [f"  │  📅 CATALYST / TIMING:", f"  │  {s['catalyst_note']}", "  │"]

    lines += [f"  │  🐂 BULL: {s.get('bull_case','')}", f"  │  🐻 BEAR: {s.get('bear_case','')}", "  │"]

    if s.get("broker_flags"):
        lines.append(f"  │  🚩 NOTES ON PRESENTATION:")
        for flag in s["broker_flags"]:
            lines.append(f"  │  • {flag}")
        lines.append(f"  │")

    if s.get("indirect_signal"):
        lines += [f"  │  💡 INDIRECT SIGNAL:", f"  │  {s['indirect_signal']}", "  │"]

    if s.get("due_diligence_checklist"):
        lines.append(f"  │  ✅ BEFORE YOU ACT:")
        for item in s["due_diligence_checklist"]:
            lines.append(f"  │  □ {item}")
        lines.append(f"  │")

    lines.append(f"  └─ VERDICT: {s.get('one_line_verdict','')}")
    return lines


def format_full_report(analysis: dict) -> str:
    thread_len    = analysis.get("_thread_length", 1)
    context_label = f"{thread_len} related emails" if thread_len > 1 else "single email"
    img_note      = f"  +{analysis.get('_image_count',0)} image(s) analyzed" if analysis.get("_image_count") else ""
    evolution_emoji = {"STRENGTHENING":"📈","STABLE":"➡️","WEAKENING":"📉","CONTRADICTORY":"⚡","N/A":""}.get(analysis.get("thesis_evolution",""),"")

    lines = [
        "═" * 64,
        "  BROKER SIGNAL ANALYSIS",
        "═" * 64,
        f"  Subject:  {analysis.get('_email_subject','')}",
        f"  Date:     {analysis.get('_email_date','')}",
        f"  Context:  {context_label}{img_note}",
        f"  Analyzed: {analysis.get('_analyzed_at','')}",
        f"  Email quality: {analysis.get('overall_email_quality','')} — {analysis.get('overall_email_quality_note','')}",
        "─" * 64,
    ]

    stocks = analysis.get("stocks", [])
    for i, stock in enumerate(stocks):
        if i > 0:
            lines.append("  · · ·")
        lines += format_stock_block(stock)

    if analysis.get("industry_comparison"):
        lines += ["", "  🏭 INDUSTRY COMPARISON:", f"  {analysis['industry_comparison']}", ""]

    if analysis.get("thread_narrative") and thread_len > 1:
        lines += ["", "  🧵 THREAD NARRATIVE:", f"  {analysis['thread_narrative']}"]
        if analysis.get("thesis_evolution") and analysis["thesis_evolution"] != "N/A":
            lines.append(f"  Trend: {evolution_emoji} {analysis['thesis_evolution']}")

    if analysis.get("urgency_escalation"):
        lines += ["", "  🚨 WARNING: Urgency escalating across emails — possible pressure tactic"]

    if analysis.get("cross_email_contradictions"):
        lines += ["", "  ⚡ CONTRADICTIONS FOUND:"]
        for c in analysis["cross_email_contradictions"]:
            lines.append(f"  • {c}")

    lines.append("═" * 64)
    return "\n".join(lines)


def save_analysis(analysis: dict, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    timestamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_subject = re.sub(r"[^\w\s-]", "", analysis.get("_email_subject", "analysis"))[:40].strip().replace(" ", "_")
    base         = f"{timestamp}_{safe_subject}"

    with open(os.path.join(output_dir, f"{base}.json"), "w") as f:
        json.dump(analysis, f, indent=2, ensure_ascii=False)

    txt_path = os.path.join(output_dir, f"{base}.txt")
    with open(txt_path, "w") as f:
        f.write(format_full_report(analysis))

    return txt_path


def print_summary(analysis: dict):
    """Print a quick console summary."""
    stocks = analysis.get("stocks", [])
    thread = analysis.get("_thread_length", 1)
    thread_note = f" (from {thread} emails)" if thread > 1 else ""
    print(f"\n{'─'*56}")
    for s in stocks:
        print(f"  📊 {s.get('ticker','?'):<12} {s.get('thesis_strength','?'):<10} ({s.get('thesis_strength_score','?')}/10)  →  {s.get('recommended_action','?')}{thread_note}")
        print(f"     {s.get('one_line_verdict','')}")
    print(f"{'─'*56}\n")


def send_notification(analysis: dict, icloud_email: str, app_password: str, notify_email: str):
    if not notify_email:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[Signal] {analysis.get('_email_subject','')[:50]}"
        msg["From"]    = icloud_email
        msg["To"]      = notify_email
        msg.attach(MIMEText(format_full_report(analysis), "plain"))

        with smtplib.SMTP("smtp.mail.me.com", 587) as server:
            server.starttls()
            server.login(icloud_email, app_password)
            server.sendmail(icloud_email, notify_email, msg.as_string())
        print(f"  [NOTIFIED] Sent to {notify_email}")
    except Exception as e:
        print(f"  [ERROR] Notification failed: {e}")
