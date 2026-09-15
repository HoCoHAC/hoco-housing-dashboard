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

  MEDIAN PRICE   HCAR / Bright MLS monthly median SOLD price for Howard
                 County, Maryland, supplied with --price and --hcar-evidence.
                 The Detailed Report and separate Insight Report must agree
                 exactly on price and completed reporting month. No threshold.
                 Moving from the legacy listing KPI is a definition/source
                 change, never a comparable market price movement.

Usage:
  python3 rate_check.py                      # rate only, on an HCAR page
  python3 rate_check.py --price 639000 --hcar-evidence /path/evidence.json
  python3 rate_check.py --no-record          # do not append to the log

Exit: 10 = something needs updating, 0 = hold, 3 = blocked pending human
      verification (missing/invalid evidence or incomplete source migration),
      1 = the check failed. Evidence is supplied by the report-verification
      workflow; this checker validates its contract, not PDF contents.
"""

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import urllib.request
from urllib.parse import unquote, urlsplit
from datetime import date

from bs4 import BeautifulSoup

PMMS_CSV = "https://www.freddiemac.com/pmms/docs/PMMS_history.csv"
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
HCAR_SOURCE = "HCAR / Bright MLS"
LEGACY_SOURCE = "legacy_realtor_listing"


def reporting_month(value, today=None):
    """Validate a YYYY-MM reporting month that has already finished."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("Reporting period must be YYYY-MM.")
    month = date(int(value[:4]), int(value[5:]), 1)
    today = today or date.today()
    if month >= today.replace(day=1):
        raise ValueError("Reporting period must be a past, completed month.")
    return value


def displayed_state(path):
    """Read only the explicit county KPI (or the exact scoped legacy KPI)."""
    with open(path, encoding="utf-8") as fh:
        html = fh.read()

    m = re.search(r"30-year fixed,\s*([\d.]+)%\s*\(week ending ([^)]+)\)", html)
    if not m:
        raise ValueError("Could not find the displayed rate in the source note.")

    soup = BeautifulSoup(html, "html.parser")
    nodes = soup.select("#county-median-sold-price")
    if nodes:
        if len(nodes) != 1:
            raise ValueError("The county median sold price KPI must be unique.")
        node = nodes[0]
        if (node.name != "div" or not {"value", "tnum"}.issubset(node.get("class", []))
                or node.get("data-source") != "hcar"):
            raise ValueError("The county median sold price KPI has invalid source markup.")
        source = HCAR_SOURCE
        metric = "median_sold_price"
        period = reporting_month(node.get("data-period"))
    else:
        # Only this exact label, inside its own KPI, is eligible for migration.
        # Never search globally for dollar amounts, community prices, or MIHU.
        labels = [label for label in soup.select(".kpi > .label")
                  if label.get_text(" ", strip=True) == "For Sale: Median Listing Price"]
        if len(labels) != 1:
            raise ValueError("Could not uniquely identify the county median price KPI.")
        node = labels[0].find_next_sibling()
        if (node is None or node.name != "div"
                or not {"value", "tnum"}.issubset(node.get("class", []))):
            raise ValueError("The legacy county price KPI has no scoped value sibling.")
        source, metric, period = LEGACY_SOURCE, "median_listing_price", None
    text = node.get_text(" ", strip=True)
    if not re.fullmatch(r"\$(?:[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d*)", text):
        raise ValueError("The county median price KPI must contain one positive dollar amount.")
    price = float(text[1:].replace(",", ""))
    return {
        "rate": float(m.group(1)), "week": m.group(2).strip(), "price": price,
        "source": source, "metric": metric, "period": period,
    }


def displayed_values(path):
    """Backward-compatible rate/week/price tuple, now using scoped extraction."""
    shown = displayed_state(path)
    return shown["rate"], shown["week"], shown["price"]


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


def positive_price(value):
    """Prices must be finite, positive JSON numbers, not strings or booleans."""
    return (type(value) in (int, float) and math.isfinite(value) and value > 0)


def validate_report_url(value, report_type):
    """Require an exact HTTPS HCAR PDF URL of the specified report type."""
    if not isinstance(value, str) or re.search(r"\s|\\", value):
        raise ValueError(f"{report_type} URL must be an HTTPS HCAR PDF URL.")
    url = urlsplit(value)
    if (url.scheme != "https" or url.netloc.lower() not in ("hcar.org", "www.hcar.org")
            or url.query or url.fragment):
        raise ValueError(f"{report_type} URL must be an exact HTTPS hcar.org PDF URL.")
    filename = unquote(url.path).rsplit("/", 1)[-1]
    # HCAR filenames vary (Detailed_Report, Detailed_Market_Report,
    # Detailed-Apr26, Insight-May26). The role is stable; the date is verified
    # through evidence metadata rather than inferred from a filename.
    role = {"Detailed_Report": "detailed", "Insight_Report": "insight"}[report_type]
    if role not in filename.lower() or not filename.lower().endswith(".pdf"):
        raise ValueError(f"Expected an HCAR {report_type} PDF, not a landing page or other report.")
    return value


def validate_hcar_evidence(evidence, price, displayed_period=None, today=None):
    """Check independently verified report metadata; do not fetch or substitute prices."""
    if not isinstance(evidence, dict):
        raise ValueError("HCAR evidence must be a JSON object.")
    for key, expected in {
        "source": HCAR_SOURCE,
        "metric": "median_sold_price",
        "geography": "Howard County, Maryland",
    }.items():
        if evidence.get(key) != expected:
            raise ValueError(f"Evidence {key} must be exactly {expected!r}.")
    if not positive_price(price):
        raise ValueError("--price must be a finite positive number.")
    for key in ("price", "verification_price"):
        if not positive_price(evidence.get(key)) or evidence[key] != price:
            raise ValueError(f"Evidence {key} must match --price exactly.")
    period = reporting_month(evidence.get("period"), today)
    if evidence.get("verification_period") != period:
        raise ValueError("The Detailed and Insight reporting periods must match exactly.")
    if displayed_period is not None:
        reporting_month(displayed_period, today)
        if period < displayed_period:
            raise ValueError("Evidence period is older than the displayed reporting period.")
    if evidence.get("report_url") == evidence.get("verification_url"):
        raise ValueError("The Detailed and Insight report URLs must be different.")
    validate_report_url(evidence.get("report_url"), "Detailed_Report")
    validate_report_url(evidence.get("verification_url"), "Insight_Report")
    return dict(evidence)


def read_hcar_evidence(path, price, displayed_period=None):
    if not path:
        raise ValueError("--price requires --hcar-evidence with matching HCAR reports.")
    with open(path, encoding="utf-8") as fh:
        evidence = json.load(fh)
    return validate_hcar_evidence(evidence, price, displayed_period)


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
        "closing_costs": round(price * 0.03),
        "cash_to_close": round(price * DOWN_PCT + price * 0.03),
    }


def load_history():
    if not os.path.exists(HISTORY):
        return []
    # Fail closed rather than replacing unreadable prior records with an empty log.
    with open(HISTORY, encoding="utf-8") as fh:
        history = json.load(fh)
    if not isinstance(history, list) or not all(isinstance(row, dict) for row in history):
        raise ValueError("History must be a list of records; refusing to overwrite it.")
    return history


def record_entry(entry, no_record):
    """Keep prior records intact and return them for the rate accumulation note."""
    history = load_history()
    if not no_record:
        with open(HISTORY, "w", encoding="utf-8") as fh:
            json.dump(history + [entry], fh, indent=2)
    return history


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", default=HTML)
    ap.add_argument("--threshold", type=float, default=0.25,
                    help="rate move required to act, in percentage points")
    ap.add_argument("--price", type=float, default=None,
                    help="HCAR / Bright MLS monthly Howard County median SOLD price")
    ap.add_argument("--hcar-evidence",
                    help="JSON containing matching Detailed and Insight report evidence")
    ap.add_argument("--no-record", action="store_true")
    args = ap.parse_args(argv)

    shown = displayed_state(args.html)
    shown_rate, shown_week, shown_price = shown["rate"], shown["week"], shown["price"]
    evidence = None
    try:
        if args.price is not None:
            evidence = read_hcar_evidence(args.hcar_evidence, args.price, shown["period"])
        elif shown["source"] == LEGACY_SOURCE:
            raise ValueError("Source migration is incomplete: the legacy listing KPI requires "
                             "--price and matching --hcar-evidence before any update.")
        elif args.hcar_evidence:
            raise ValueError("--hcar-evidence requires --price.")
    except (ValueError, OSError, OverflowError) as exc:
        entry = {
            "checked": date.today().isoformat(), "action": "blocked",
            "displayed_rate": shown_rate, "displayed_week": shown_week,
            "displayed_price": shown_price, "displayed_price_source": shown["source"],
            "displayed_price_period": shown["period"], "displayed_price_metric": shown["metric"],
            "observed_price": args.price if positive_price(args.price) else None,
            "observed_rate": None, "observed_week": None, "gap": None,
            "threshold": args.threshold, "rate_crossed": False, "price_changed": False,
            "source_changed": False, "advance_updated_badge": False,
            "hcar_evidence": None, "blocked_reason": str(exc),
        }
        print(f"DECISION: BLOCKED — {exc}")
        print("          Do not edit the dashboard. Supply matching verified HCAR Detailed "
              "and Insight report evidence and rerun.")
        try:
            record_entry(entry, args.no_record)
        except (ValueError, OSError) as history_exc:
            # Invalid evidence must remain exit 3 even when the audit log is unavailable.
            print(f"WARNING: history could not be recorded: {history_exc}", file=sys.stderr)
        return 3

    obs_rate, obs_week = latest_pmms()

    gap = round(obs_rate - shown_rate, 4)
    rate_crossed = abs(gap) >= args.threshold

    obs_price = args.price
    source_changed = evidence is not None and shown["source"] == LEGACY_SOURCE
    numeric_price_changed = obs_price is not None and obs_price != shown_price
    # Only prices from the same definition can be reported as market movement.
    price_changed = numeric_price_changed and not source_changed
    period_advanced = (evidence is not None and shown["period"] is not None
                       and evidence["period"] > shown["period"])

    # Never substitute a rate that did not cross the threshold.
    eff_rate = obs_rate if rate_crossed else shown_rate
    eff_price = obs_price if evidence is not None else shown_price
    update = rate_crossed or price_changed or source_changed

    print(f"Displayed rate         : {shown_rate:.2f}%  (week ending {shown_week})")
    print(f"Freddie Mac PMMS latest: {obs_rate:.2f}%  (week ending {obs_week})")
    print(f"Gap vs displayed       : {gap:+.2f} pt   (threshold {args.threshold:.2f})"
          f"  -> {'CROSSED' if rate_crossed else 'under'}")
    print()
    print(f"Displayed median price : ${shown_price:,.0f}")
    print(f"Displayed price source : {shown['source']} ({shown['period'] or 'period unknown'})")
    if obs_price is None:
        print("Observed median price  : not supplied (pass --price to check it)")
    else:
        print(f"Observed median price  : ${obs_price:,.0f}"
              f"  -> {'DEFINITION/SOURCE CHANGE' if source_changed else ('CHANGED' if price_changed else 'unchanged')}")
        print(f"HCAR evidence          : matching median SOLD price, {evidence['period']}")

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
        "numeric_price_changed": numeric_price_changed,
        "source_changed": source_changed,
        "change_type": "definition/source change" if source_changed else (
            "market price movement" if price_changed else None),
        "displayed_price_source": shown["source"],
        "displayed_price_metric": shown["metric"],
        "displayed_price_period": shown["period"],
        "observed_price_source": HCAR_SOURCE if evidence is not None else None,
        "observed_price_metric": "median_sold_price" if evidence is not None else None,
        "observed_price_period": evidence["period"] if evidence is not None else None,
        "hcar_evidence": evidence,
        "period_advanced": period_advanced,
        "metadata_refresh_only": period_advanced and not update,
        "advance_updated_badge": update,
        "effective_rate": eff_rate,
        "effective_price": eff_price,
        "action": "update" if update else "hold",
    }
    prior = record_entry(entry, args.no_record)

    consecutive = 0
    for past in reversed(prior):
        if past.get("action") == "hold" and abs(past.get("gap", 0)) >= 0.15:
            consecutive += 1
        else:
            break

    print()
    if update:
        reasons = []
        if rate_crossed:
            reasons.append(f"rate moved {gap:+.2f} pt")
        if source_changed:
            reasons.append("definition/source change: legacy Realtor.com median LISTING "
                           "price → HCAR / Bright MLS median SOLD price (not market movement)")
        elif price_changed:
            reasons.append(f"price moved ${obs_price - shown_price:+,.0f}")
        print(f"DECISION: UPDATE — {'; '.join(reasons)}.")
        if (price_changed or source_changed) and not rate_crossed:
            print(f"          Recalculating at the OLD displayed rate of {shown_rate:.2f}%, "
                  f"since {obs_rate:.2f}% did not cross the threshold.")
        print()
        before = monthly_figures(shown_price, shown_rate)
        after = monthly_figures(eff_price, eff_rate)
        print(f"  rate used            {shown_rate:>13.2f}% {eff_rate:>15.2f}%")
        print(f"  median price         {shown_price:>13,.0f}  {eff_price:>14,.0f}")
        print(f"  {'figure':<19}{'current':>14}{'recalculated':>16}")
        for key, label in [("loan", "Loan"), ("pi", "P&I"), ("tax", "Property tax"),
                           ("insurance", "Insurance"),
                           ("pmi", "PMI"), ("total_monthly", "Total monthly"),
                           ("income_required", "Income required"),
                           ("down_payment", "Down payment"),
                           ("closing_costs", "Closing costs"),
                           ("cash_to_close", "Cash to close")]:
            mark = " " if before[key] == after[key] else "*"
            print(f"{mark} {label:<19}{before[key]:>13,}{after[key]:>16,}")
        print("\n  (* = changed)")
    else:
        print(f"DECISION: HOLD — nothing to change. Rate gap {abs(gap):.2f} pt is under "
              f"the {args.threshold:.2f} pt threshold"
              + ("; price unchanged." if obs_price is not None else "."))
        if period_advanced:
            print(f"NOTE: report period advanced to {evidence['period']} with no material "
                  "update. Metadata may be refreshed; do not advance the Updated badge.")
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
