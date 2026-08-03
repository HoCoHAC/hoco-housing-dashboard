#!/usr/bin/env python3
"""
Deterministic refresh check for the Howard County Housing Dashboard.

Decides whether the homebuying card needs updating, and if so computes every
dependent figure using the page's own formulas and rounding convention.

Two inputs, two different rules:

  MORTGAGE RATE  Fetched automatically from the official Freddie Mac PMMS
                 history. Updated only when it has moved a quarter point or
                 more from the rate CURRENTLY DISPLAYED on the dashboard —
                 measured against what is displayed, never against the last
                 reading, so small monthly moves accumulate until they matter.

  MEDIAN PRICE   Read off the Realtor.com market trends page by the monthly
                 run and passed in with --price. No threshold; any change
                 counts. Optionally sanity-checked against Realtor.com's own
                 county research file, which is a lagged monthly aggregate —
                 useful as a guard against a misread, NOT as a source.

Usage:
  python3 rate_check.py                      # rate only
  python3 rate_check.py --price 692000       # rate + observed price
  python3 rate_check.py --no-record          # do not append to the log

Exit: 10 = something needs updating, 0 = hold, 3 = blocked pending human
      verification (observed price looks implausible), 1 = the check failed.
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import urllib.request
from datetime import date

PMMS_CSV = "https://www.freddiemac.com/pmms/docs/PMMS_history.csv"
RDC_CSV = ("https://econdata.s3-us-west-2.amazonaws.com/Reports/Core/"
           "RDC_Inventory_Core_Metrics_County.csv")
HTML = "/home/user/workspace/hoco-dashboard/index.html"
HISTORY = "/home/user/workspace/rate_history.json"
UA = {"User-Agent": "Mozilla/5.0 (dashboard-refresh)"}

# Cost assumptions from the dashboard's Methodology section.
DOWN_PCT = 0.10
TAX_RATE = 0.01014
INSURANCE_MONTHLY = 150.0
PMI_RATE = 0.0055
INCOME_RATIO = 0.28
TERM_MONTHS = 360


def displayed_values(path):
    """The rate and median price currently written into the dashboard."""
    with open(path, encoding="utf-8") as fh:
        html = fh.read()

    m = re.search(r"30-year fixed,\s*([\d.]+)%\s*\(week ending ([^)]+)\)", html)
    if not m:
        raise SystemExit("Could not find the displayed rate in the source note.")

    prices = re.findall(r"\$(\d{3},\d{3})\b", html)
    if not prices:
        raise SystemExit("Could not find the displayed median price.")
    price = float(max(set(prices), key=prices.count).replace(",", ""))

    return float(m.group(1)), m.group(2).strip(), price


def latest_pmms(timeout=45):
    """Latest 30-year fixed rate and its week-ending date."""
    req = urllib.request.Request(PMMS_CSV, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    rows = [r for r in csv.DictReader(io.StringIO(raw))
            if (r.get("pmms30") or "").strip()]
    if not rows:
        raise SystemExit("PMMS history returned no usable rows.")
    return float(rows[-1]["pmms30"].strip()), rows[-1]["date"].strip()


def research_price(timeout=90):
    """
    Realtor.com's county research file — a LAGGED MONTHLY AGGREGATE.

    Deliberately not the dashboard's source: it runs roughly two months
    behind the live market page and is a different series, so its value
    will not match exactly. Used only to catch an implausible reading.
    Returns (price, month) or (None, reason).
    """
    try:
        req = urllib.request.Request(RDC_CSV, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8-sig", errors="replace")
        for row in csv.DictReader(io.StringIO(raw)):
            name = (row.get("county_name") or "").lower()
            if "howard" in name and ", md" in name:
                val = (row.get("median_listing_price") or "").strip()
                if val:
                    return float(val), (row.get("month_date_yyyymm") or "").strip()
        return None, "Howard County row not found"
    except Exception as exc:  # noqa: BLE001
        return None, f"unavailable ({type(exc).__name__})"


def monthly_figures(price, rate_pct):
    """Every figure on the homebuying card, derived in one place."""
    loan = price * (1 - DOWN_PCT)
    r = rate_pct / 100 / 12
    pi = loan * r / (1 - (1 + r) ** -TERM_MONTHS) if r else loan / TERM_MONTHS
    tax = price * TAX_RATE / 12
    pmi = loan * PMI_RATE / 12
    # Sum the ROUNDED components, matching how the cost table is displayed,
    # so the column adds up visually on the page.
    total = round(pi) + round(tax) + round(INSURANCE_MONTHLY) + round(pmi)
    return {
        "loan": round(loan),
        "pi": round(pi),
        "tax": round(tax),
        "insurance": round(INSURANCE_MONTHLY),
        "pmi": round(pmi),
        "total_monthly": total,
        "income_required": round(total / INCOME_RATIO * 12 / 50) * 50,
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
    ap.add_argument("--threshold", type=float, default=0.25,
                    help="rate move required to act, in percentage points")
    ap.add_argument("--price", type=float, default=None,
                    help="median listing price observed on Realtor.com this run")
    ap.add_argument("--tolerance", type=float, default=10.0,
                    help="%% divergence from the research file that triggers a warning")
    ap.add_argument("--no-crosscheck", action="store_true")
    ap.add_argument("--no-record", action="store_true")
    args = ap.parse_args()

    shown_rate, shown_week, shown_price = displayed_values(args.html)
    obs_rate, obs_week = latest_pmms()

    gap = round(obs_rate - shown_rate, 4)
    rate_crossed = abs(gap) >= args.threshold

    obs_price = args.price
    price_changed = obs_price is not None and obs_price != shown_price

    # Never substitute a rate that did not cross the threshold.
    eff_rate = obs_rate if rate_crossed else shown_rate
    eff_price = obs_price if price_changed else shown_price
    update = rate_crossed or price_changed

    print(f"Displayed rate         : {shown_rate:.2f}%  (week ending {shown_week})")
    print(f"Freddie Mac PMMS latest: {obs_rate:.2f}%  (week ending {obs_week})")
    print(f"Gap vs displayed       : {gap:+.2f} pt   (threshold {args.threshold:.2f})"
          f"  -> {'CROSSED' if rate_crossed else 'under'}")
    print()
    print(f"Displayed median price : ${shown_price:,.0f}")
    if obs_price is None:
        print("Observed median price  : not supplied (pass --price to check it)")
    else:
        print(f"Observed median price  : ${obs_price:,.0f}"
              f"  -> {'CHANGED' if price_changed else 'unchanged'}")

    # Sanity guard on the browsed price. Never sets the value.
    crosscheck = None
    blocked = False
    if obs_price is not None and not args.no_crosscheck:
        ref, meta = research_price()
        if ref is None:
            print(f"Cross-check            : skipped, research file {meta}")
        else:
            div = (obs_price - ref) / ref * 100
            crosscheck = {"reference": ref, "month": meta, "divergence_pct": round(div, 2)}
            flag = abs(div) > args.tolerance
            blocked = flag
            print(f"Cross-check            : research file ${ref:,.0f} ({meta}), "
                  f"observed differs by {div:+.1f}%"
                  f"  -> {'IMPLAUSIBLE' if flag else 'plausible'}")
            if flag:
                print()
                print(f"WARNING: the observed price is more than {args.tolerance:.0f}% from "
                      f"Realtor.com's own county research figure. That file lags by about "
                      f"two months and is a different series, so some divergence is normal "
                      f"— but this much suggests a misread. Verify the page by hand before "
                      f"publishing anything.")

    history = load_history()
    entry = {
        "checked": date.today().isoformat(),
        "displayed_rate": shown_rate,
        "displayed_week": shown_week,
        "observed_rate": obs_rate,
        "observed_week": obs_week,
        "gap": gap,
        "threshold": args.threshold,
        "rate_crossed": rate_crossed,
        "displayed_price": shown_price,
        "observed_price": obs_price,
        "price_changed": price_changed,
        "crosscheck": crosscheck,
        "action": "blocked" if blocked else ("update" if update else "hold"),
    }
    if not args.no_record:
        history.append(entry)
        with open(HISTORY, "w", encoding="utf-8") as fh:
            json.dump(history, fh, indent=2)

    consecutive = 0
    prior = history[:-1] if not args.no_record else history
    for past in reversed(prior):
        if past.get("action") == "hold" and abs(past.get("gap", 0)) >= 0.15:
            consecutive += 1
        else:
            break

    print()
    if blocked:
        print("DECISION: BLOCKED — not updating anything. The observed price failed "
              "the plausibility check above.")
        print("          Do not edit the dashboard on this reading. Confirm the figure "
              "on the Realtor.com page by hand,")
        print("          then rerun with the verified value. If the page really does "
              "show this, pass --no-crosscheck.")
        return 3

    if update:
        reasons = []
        if rate_crossed:
            reasons.append(f"rate moved {gap:+.2f} pt")
        if price_changed:
            reasons.append(f"price moved ${obs_price - shown_price:+,.0f}")
        print(f"DECISION: UPDATE — {'; '.join(reasons)}.")
        if price_changed and not rate_crossed:
            print(f"          Recalculating at the OLD displayed rate of {shown_rate:.2f}%, "
                  f"since {obs_rate:.2f}% did not cross the threshold.")
        print()
        before = monthly_figures(shown_price, shown_rate)
        after = monthly_figures(eff_price, eff_rate)
        print(f"  rate used            {shown_rate:>13.2f}% {eff_rate:>15.2f}%")
        print(f"  median price         {shown_price:>13,.0f}  {eff_price:>14,.0f}")
        print(f"  {'figure':<19}{'current':>14}{'recalculated':>16}")
        for key, label in [("pi", "P&I"), ("tax", "Property tax"),
                           ("pmi", "PMI"), ("total_monthly", "Total monthly"),
                           ("income_required", "Income required"),
                           ("down_payment", "Down payment"),
                           ("cash_to_close", "Cash to close")]:
            mark = " " if before[key] == after[key] else "*"
            print(f"{mark} {label:<19}{before[key]:>13,}{after[key]:>16,}")
        print("\n  (* = changed)")
    else:
        print(f"DECISION: HOLD — nothing to change. Rate gap {abs(gap):.2f} pt is under "
              f"the {args.threshold:.2f} pt threshold"
              + ("; price unchanged." if obs_price is not None else "."))
        if consecutive >= 2:
            print()
            print(f"NOTE: the rate gap has been 0.15 pt or wider for {consecutive + 1} "
                  f"consecutive checks without crossing. Worth a look by hand.")

    return 10 if update else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
