#!/usr/bin/env python3
"""
Deterministic threshold check for the Howard County Housing Dashboard.

Reads the rate currently DISPLAYED on the dashboard, fetches the latest
Freddie Mac PMMS 30-year fixed rate, and decides whether the quarter-point
threshold has been crossed.

The gap is always measured against the displayed rate, never against the
previous observation, so small monthly moves accumulate until they matter.

Also recomputes every rate-dependent figure so the arithmetic is done once,
in one place, rather than re-derived by hand each month.

Usage:  python3 rate_check.py [--html PATH] [--threshold 0.25]
Exit:   10 = update needed, 0 = hold, 1 = error
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, date

PMMS_CSV = "https://www.freddiemac.com/pmms/docs/PMMS_history.csv"
HTML = "/home/user/workspace/hoco-dashboard/index.html"
HISTORY = "/home/user/workspace/rate_history.json"

# Cost assumptions baked into the dashboard's Methodology section.
DOWN_PCT = 0.10
TAX_RATE = 0.01014          # effective annual property tax rate
INSURANCE_MONTHLY = 150.0
PMI_RATE = 0.0055           # annual, on loan balance
INCOME_RATIO = 0.28         # housing costs as share of gross income
TERM_MONTHS = 360


def displayed_values(path):
    """Pull the rate and median price currently written into the dashboard."""
    with open(path, encoding="utf-8") as fh:
        html = fh.read()

    m = re.search(
        r"30-year fixed,\s*([\d.]+)%\s*\(week ending ([^)]+)\)", html)
    if not m:
        raise SystemExit("Could not find the displayed rate in the source note.")
    rate, week = float(m.group(1)), m.group(2).strip()

    prices = re.findall(r"\$(\d{3},\d{3})\b", html)
    if not prices:
        raise SystemExit("Could not find the displayed median price.")
    price = float(max(set(prices), key=prices.count).replace(",", ""))

    return rate, week, price


def latest_pmms(timeout=45):
    """Latest 30-year fixed rate and its week-ending date."""
    req = urllib.request.Request(
        PMMS_CSV, headers={"User-Agent": "Mozilla/5.0 (dashboard-refresh)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")

    rows = [r for r in csv.DictReader(io.StringIO(raw))
            if (r.get("pmms30") or "").strip()]
    if not rows:
        raise SystemExit("PMMS history returned no usable rows.")

    last = rows[-1]
    return float(last["pmms30"].strip()), last["date"].strip()


def monthly_figures(price, rate_pct):
    """Every rate-dependent figure on the homebuying card, from one place."""
    loan = price * (1 - DOWN_PCT)
    r = rate_pct / 100 / 12
    pi = loan * r / (1 - (1 + r) ** -TERM_MONTHS) if r else loan / TERM_MONTHS
    tax = price * TAX_RATE / 12
    pmi = loan * PMI_RATE / 12
    # Sum the ROUNDED components, matching how the cost table is displayed,
    # so the column adds up visually on the page.
    total = round(pi) + round(tax) + round(INSURANCE_MONTHLY) + round(pmi)
    income = total / INCOME_RATIO * 12
    return {
        "loan": round(loan),
        "pi": round(pi),
        "tax": round(tax),
        "insurance": round(INSURANCE_MONTHLY),
        "pmi": round(pmi),
        "total_monthly": total,
        "income_required": round(income / 50) * 50,
        "down_payment": round(price * DOWN_PCT),
        "cash_to_close": round(price * DOWN_PCT + price * 0.03),
    }


def load_history():
    if not os.path.exists(HISTORY):
        return []
    try:
        with open(HISTORY, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", default=HTML)
    ap.add_argument("--threshold", type=float, default=0.25)
    ap.add_argument("--no-record", action="store_true",
                    help="check without appending to the history log")
    args = ap.parse_args()

    shown_rate, shown_week, price = displayed_values(args.html)
    obs_rate, obs_week = latest_pmms()

    gap = round(obs_rate - shown_rate, 4)
    crossed = abs(gap) >= args.threshold

    history = load_history()
    entry = {
        "checked": date.today().isoformat(),
        "displayed_rate": shown_rate,
        "displayed_week": shown_week,
        "observed_rate": obs_rate,
        "observed_week": obs_week,
        "gap": gap,
        "threshold": args.threshold,
        "action": "update" if crossed else "hold",
    }
    if not args.no_record:
        history.append(entry)
        with open(HISTORY, "w", encoding="utf-8") as fh:
            json.dump(history, fh, indent=2)

    # How long has this gap been building without crossing?
    consecutive = 0
    for past in reversed(history[:-1] if not args.no_record else history):
        if past.get("action") == "hold" and abs(past.get("gap", 0)) >= 0.15:
            consecutive += 1
        else:
            break

    before = monthly_figures(price, shown_rate)
    after = monthly_figures(price, obs_rate)

    print(f"Displayed on dashboard : {shown_rate:.2f}%  (week ending {shown_week})")
    print(f"Freddie Mac PMMS latest: {obs_rate:.2f}%  (week ending {obs_week})")
    print(f"Median price displayed : ${price:,.0f}")
    print(f"Gap vs displayed       : {gap:+.2f} pt   (threshold {args.threshold:.2f})")
    print()

    if crossed:
        print("DECISION: UPDATE — the gap has reached the threshold.")
        print()
        print(f"{'figure':<20}{'current':>14}{'recalculated':>16}")
        for key, label in [("pi", "P&I"), ("tax", "Property tax"),
                           ("pmi", "PMI"), ("total_monthly", "Total monthly"),
                           ("income_required", "Income required")]:
            print(f"{label:<20}{before[key]:>14,}{after[key]:>16,}")
    else:
        print(f"DECISION: HOLD — {abs(gap):.2f} pt is under the "
              f"{args.threshold:.2f} pt threshold. Leave the figures alone.")
        if consecutive >= 2:
            print()
            print(f"NOTE: the gap has been 0.15 pt or wider for "
                  f"{consecutive + 1} consecutive checks without crossing. "
                  f"Worth a look by hand.")

    return 10 if crossed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
