#!/usr/bin/env python3
"""
run.py — Broker Signal Tracker entry point.

Usage:
  python3 run.py                # continuous polling loop
  python3 run.py --once         # single pass then exit
  python3 run.py --once --limit 3  # analyze at most 3 new threads
  python3 run.py --scores       # print broker accuracy scorecard and exit
  python3 run.py --reset        # clear seen_emails.json to reprocess all emails
"""

import sys
import os
import json
import time
import subprocess
from datetime import datetime, timezone, timedelta

ANALYSIS_DELAY_SEC = 15  # pause between analyses to respect rate limits

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

# ── CONFIG ─────────────────────────────────────────────────────────────────────
ICLOUD_EMAIL        = os.getenv("ICLOUD_EMAIL", "")
ICLOUD_APP_PASSWORD = os.getenv("ICLOUD_APP_PASSWORD", "")
BROKER_SENDER       = os.getenv("BROKER_SENDER", "")
BROKER_NAME         = os.getenv("BROKER_NAME", "your broker")
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
POLL_INTERVAL_SEC   = int(os.getenv("POLL_INTERVAL_SEC", "300"))
NOTIFY_EMAIL        = os.getenv("NOTIFY_EMAIL", "")
OUTPUT_DIR          = os.getenv("OUTPUT_DIR", "./analyses")
DATA_DIR            = os.getenv("DATA_DIR", "./data")
SEEN_FILE           = os.path.join(DATA_DIR, "seen_emails.json")
# ───────────────────────────────────────────────────────────────────────────────

# Override memory/tracker file paths from DATA_DIR
import memory as mem_module
import broker_tracker as tracker_module
mem_module.MEMORY_FILE       = os.path.join(DATA_DIR, "memory.json")
tracker_module.TRACKER_FILE  = os.path.join(DATA_DIR, "broker_track.json")


def validate_config():
    missing = [k for k in ["ICLOUD_EMAIL", "ICLOUD_APP_PASSWORD", "ANTHROPIC_API_KEY"] if not os.getenv(k)]
    if missing:
        print(f"[ERROR] Missing required environment variables: {', '.join(missing)}")
        print("  → Copy .env.example to .env and fill in your values.")
        sys.exit(1)


def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE) as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def save_seen(seen: set):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


def run_once(limit: int = 0, max_days: int = 0):
    from email_fetcher import fetch_all_broker_emails, build_threads
    from analyzer import analyze_thread
    from reporter import save_analysis, print_summary, send_notification

    seen = load_seen()

    all_emails = fetch_all_broker_emails(ICLOUD_EMAIL, ICLOUD_APP_PASSWORD, BROKER_SENDER)

    # Optional date window filter
    if max_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)
        before = len(all_emails)
        all_emails = [e for e in all_emails if e.get("datetime") and e["datetime"] >= cutoff]
        skipped = before - len(all_emails)
        if skipped:
            print(f"  Skipped {skipped} email(s) older than {max_days} days")

    new_emails = [e for e in all_emails if e["hash"] not in seen]

    if not new_emails:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] No new emails.")
        # Still run daily reeval + dashboard refresh
        script_dir = os.path.dirname(os.path.abspath(__file__))
        _run_reeval_if_needed(ANTHROPIC_API_KEY, script_dir)
        return

    all_threads   = build_threads(all_emails)
    analyzed_keys = set()
    analyses_done = 0

    total = f"{limit} of {len(new_emails)}" if limit else str(len(new_emails))
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {total} new email(s) to analyze...")

    for new_em in new_emails:
        key = new_em["thread_key"]
        if key in analyzed_keys:
            continue
        analyzed_keys.add(key)

        thread = all_threads.get(key, [new_em])
        label  = new_em["subject"][:55]
        if len(thread) > 1:
            print(f"\n  Thread '{label}' ({len(thread)} emails):")
        else:
            print(f"\n  Email '{label}':")

        analysis = analyze_thread(thread, BROKER_NAME, ANTHROPIC_API_KEY)

        if analysis:
            txt_path = save_analysis(analysis, OUTPUT_DIR)
            print_summary(analysis)
            send_notification(analysis, ICLOUD_EMAIL, ICLOUD_APP_PASSWORD, NOTIFY_EMAIL)
            print(f"  Saved: {txt_path}")
            analyses_done += 1

            # Only mark as seen after successful analysis
            for em in thread:
                seen.add(em["hash"])
            save_seen(seen)
        else:
            print("  [SKIP] Analysis failed — will retry next run.")

        if limit and analyses_done >= limit:
            print(f"\n  [LIMIT] Reached --limit {limit}, stopping.")
            break

        time.sleep(ANALYSIS_DELAY_SEC)

    script_dir = os.path.dirname(os.path.abspath(__file__))

    if analyses_done > 0:
        subprocess.run([sys.executable, os.path.join(script_dir, "dashboard.py")], cwd=script_dir)
        print("  Dashboard regenerated → dashboard.html")

    # Re-evaluate tracked tickers (once per day max)
    _run_reeval_if_needed(ANTHROPIC_API_KEY, script_dir)


def _run_reeval_if_needed(api_key: str, script_dir: str = None):
    """Run reeval only if not already done today."""
    import json as _json
    reeval_file = os.path.join(script_dir or ".", "data", "reeval.json")
    if os.path.exists(reeval_file):
        try:
            with open(reeval_file, "r") as f:
                data = _json.load(f)
            # Check timestamp of any entry
            for entry in data.values():
                ts = entry.get("timestamp", "")
                if ts[:10] == datetime.now().strftime("%Y-%m-%d"):
                    print("\n  [RE-EVAL] Already done today — skipping (run --reeval to force)")
                    return
                break  # only need to check one
        except Exception:
            pass
    _run_reeval(api_key, script_dir)


def _run_reeval(api_key: str, script_dir: str = None):
    """Run price refresh + batch thesis re-evaluation for active tickers."""
    from reeval import refresh_prices, batch_reeval, store_reeval_results

    if script_dir is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))

    print("\n  [RE-EVAL] Updating prices for tracked tickers...")
    active = refresh_prices()
    print(f"  [RE-EVAL] {len(active)} active non-stale ticker(s)")

    if active:
        print(f"  [RE-EVAL] Batch thesis check...")
        results = batch_reeval(active, api_key)
        store_reeval_results(results, active)

        for t, r in results.items():
            flag = " *** ALERT ***" if r.get("alert") else ""
            signal = f" [{r['action_signal']}]" if r.get("action_signal") not in (None, "HOLD") else ""
            print(f"    {t}: {r['evolution']}{signal} — {r['note']}{flag}")

        # Re-generate dashboard with evolution data
        subprocess.run([sys.executable, os.path.join(script_dir, "dashboard.py")], cwd=script_dir)
        print("  Dashboard updated with re-eval data")
    else:
        print("  [RE-EVAL] No active tickers to evaluate")


def run_loop():
    validate_config()
    print(f"🔍 Broker Signal Tracker running (every {POLL_INTERVAL_SEC}s)")
    print(f"   Broker:  {BROKER_NAME} (filter: '{BROKER_SENDER}')")
    print(f"   iCloud:  {ICLOUD_EMAIL}")
    print(f"   Output:  {os.path.abspath(OUTPUT_DIR)}")
    print(f"   Data:    {os.path.abspath(DATA_DIR)}\n")

    while True:
        try:
            run_once(limit=0)
        except KeyboardInterrupt:
            print("\n[STOPPED]")
            break
        except Exception as e:
            print(f"[ERROR] {e}")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    if "--scores" in sys.argv:
        from broker_tracker import get_scorecard
        print(get_scorecard())

    elif "--reset" in sys.argv:
        if os.path.exists(SEEN_FILE):
            os.remove(SEEN_FILE)
            print(f"[RESET] Cleared {SEEN_FILE} — all emails will be reprocessed on next run.")
        else:
            print("[RESET] Nothing to clear.")

    elif "--reeval" in sys.argv:
        validate_config()
        _run_reeval(ANTHROPIC_API_KEY)

    elif "--once" in sys.argv:
        validate_config()
        limit = 0
        max_days = 14  # default: only look at last 2 weeks
        if "--limit" in sys.argv:
            idx = sys.argv.index("--limit")
            if idx + 1 < len(sys.argv):
                limit = int(sys.argv[idx + 1])
        if "--days" in sys.argv:
            idx = sys.argv.index("--days")
            if idx + 1 < len(sys.argv):
                max_days = int(sys.argv[idx + 1])
        run_once(limit=limit, max_days=max_days)

    else:
        run_loop()
