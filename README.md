# Howard County Housing Dashboard

Source of record for the Housing Affordability Coalition's Howard County housing dashboard.

**Live site:** https://hoco-housing-affordability-coalition.pplx.app
**Standby mirror:** https://hocohac.github.io/hoco-housing-dashboard/

Maintained by the [Housing Affordability Coalition of Howard County](https://www.acshoco.org).

---

## What's in this repository

| File | What it is |
| --- | --- |
| `index.html` | The dashboard. This is the whole site — one self-contained file. Loads Inter from Google Fonts and Chart.js from a CDN. |
| `logo-hac.png` | Coalition logo used in the header. |
| `index-offline.html` | Identical dashboard with zero external requests. Works with no internet connection. |
| `chart.umd.min.js` | Chart.js 4.4.1, bundled locally so the offline version can render its charts. |
| `make_pdf.py` | Builds a dated PDF from the current offline dashboard, checks all charts, blocks external requests, and refuses to overwrite an existing snapshot. |

There is no build step, no framework, and no server. `index.html` is plain HTML, CSS, and JavaScript, and all figures are written directly into the file.

---

## How to restore the dashboard if the live site disappears

Pick whichever of these fits the situation.

**Fastest — use the mirror.** https://hocohac.github.io/hoco-housing-dashboard/ is already live and serves the same dashboard. Point people there. Nothing needs to be rebuilt.

**Open it locally.** Download `index-offline.html`, `chart.umd.min.js`, and `logo-hac.png` into the same folder, then double-click `index-offline.html`. It opens in any browser and works with no internet.

**Rehost it anywhere.** Upload `index.html` and `logo-hac.png` to any static web host — Netlify, Cloudflare Pages, an S3 bucket, or a folder on the Coalition's own web server. There is nothing to configure.

---

## How to edit a number by hand

Every figure lives as plain text inside `index.html`. Open it in any text editor, search for the number you want to change, edit it, and save. Then commit the change here so the history stays accurate.

Two things to keep in mind:

- The three homebuying figures — principal and interest, total monthly payment, and income required — are all derived from the mortgage rate. If the rate changes, all three have to be recalculated together or the card will contradict itself.
- The "Data as of" badge in the header and the matching line in the footer both need updating whenever figures change.

---

## Data sources

All figures come from public government and industry sources. The Full Source List at the bottom groups direct links and caveats in card order; the primary sources are:

- [HUD Comprehensive Housing Affordability Strategy (CHAS)](https://www.huduser.gov/portal/datasets/cp.html) — 2018–2022 release, Table 9, for cost burden by race and ethnicity
- [U.S. Census Bureau American Community Survey](https://data.census.gov) — tables B25003, B25070, and B25091, 2020–2024 5-year estimates
- [Howard County Rental Housing Survey, June 2024](https://www.howardcountymd.gov/housing-community-development/publications-reports) — Real Property Research Group, Table 64, for the rental supply gap
- [Freddie Mac Primary Mortgage Market Survey](https://www.freddiemac.com/pmms) — weekly 30-year fixed mortgage rate
- [HCAR Monthly Stats](https://www.hcar.org/pages/monthly-stats/): monthly countywide median sold price, based on Bright MLS closed sales, for Key Metrics and the homebuying scenario
- [Realtor.com market data](https://www.realtor.com/local/market/maryland/howard-county): separate community listing-price snapshot and same-definition county reference; not updated by the HCAR refresh
- [Howard County Housing Commission](https://www.howardcountymd.gov/housing-community-development) — payment standards and Moderate Income Housing Unit program data

The Methodology section at the bottom of the dashboard documents how each figure is calculated and every caveat that applies.

## Monthly market refresh

Read the latest completed-month overall median sold price from HCAR's Detailed Report, and cross-check the same month and value against its Local Market Insight Report. Discover links from the Monthly Stats page; do not guess filenames, which have varied. Image-only PDFs need visual reading or OCR. Do not use average price, year-to-date median, or a property-type subset.

Run `python3 rate_check.py --price <verified_price> --hcar-evidence <evidence.json>`. The evidence file records the metric, geography, period, source, both HCAR report URLs, and matching values. See `hcar-evidence-2026-08.json` for a verified example. The checker blocks missing or inconsistent evidence and never compares sold prices against Realtor.com listing prices. If HCAR cannot be verified, omit both price arguments for a rate-only check on an already-migrated dashboard.

The checker controls the 0.25-percentage-point mortgage threshold measured against the displayed rate. Use every printed calculation verbatim, including the cost chart dataset, its accessible description, table, prose, loan, down payment, closing costs, and cash to close. Never pick the county price by its frequency in the HTML: the stable `county-median-sold-price` element identifies it separately from community and MIHU values.

Changing from Realtor.com listing price to HCAR sold price is a source/definition change, not a measured market decline. Preserve historical records with their original source. A report-month-only change does not advance the “Data as of” badge. The published HCAR period must match the report actually supporting the displayed price.

Local preview changes do not authorize publication. After approval, publish and synchronize the GitHub mirror, offline copy, and newly dated archives together; preserve earlier backups.

---

## Backup arrangement

This repository is the system of record and holds the full revision history of every change.

A parallel copy lives in the Coalition's Google Drive under **Housing Dashboard — Backup**. That folder holds the double-click offline version, dated PDF snapshots, and plain-language restore instructions, so someone who has never used GitHub can still find and use the dashboard.
