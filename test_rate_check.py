"""Offline regression tests for the HCAR checker migration.

Run: PYTHONDONTWRITEBYTECODE=1 python -m unittest -v test_rate_check
All HTML, evidence, history writes, and PMMS responses are in-memory mocks.
"""

import copy
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from unittest.mock import mock_open, patch

import rate_check as rc


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 15)


REPORT_ROOT = ("https://www.hcar.org/clientuploads/PDF%20Files/Housing_Stats/"
               "2026_Housing_Stats/August/")
DETAILED = REPORT_ROOT + "August_2026_Detailed_Report_(1).pdf"
INSIGHT = REPORT_ROOT + "August_2026_Local_Market_Insight_Report.pdf"
RATE_NOTE = "<li>30-year fixed, 6.66% (week ending July 30, 2026)</li>"
HCAR_KPI = ('<div class="value tnum" id="county-median-sold-price" '
            'data-source="hcar" data-period="2026-08">$639,000</div>')
LEGACY_KPI = ('<div class="kpi"><div class="label">For Sale: Median Listing Price</div>'
              '<!-- price follows only this label -->'
              '<div class="value tnum">$657,475</div>'
              '<div class="sub">Realtor.com, September 2026</div></div>')
COMPETING = (
    '<section id="communities"><p>Community home price $657,475</p></section>' * 80
    + '<div class="kpi"><div class="label">MIHU Maximum Home Price</div>'
      '<div class="value tnum">$400,000</div></div>'
)


def evidence(**changes):
    result = {
        "source": "HCAR / Bright MLS",
        "metric": "median_sold_price",
        "geography": "Howard County, Maryland",
        "period": "2026-08",
        "price": 639000,
        "verification_price": 639000,
        "verification_period": "2026-08",
        "report_url": DETAILED,
        "verification_url": INSIGHT,
    }
    result.update(changes)
    return result


def state(legacy=False, **changes):
    result = {
        "rate": 6.66, "week": "July 30, 2026",
        "price": 657475.0 if legacy else 639000.0,
        "source": rc.LEGACY_SOURCE if legacy else rc.HCAR_SOURCE,
        "metric": "median_listing_price" if legacy else "median_sold_price",
        "period": None if legacy else "2026-08",
    }
    result.update(changes)
    return result


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(rc, "date", FixedDate)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.network = patch.object(
            rc.urllib.request, "urlopen",
            side_effect=AssertionError("Unit tests must not use the network."),
        ).start()
        self.addCleanup(patch.stopall)


class ExtractionTests(OfflineTestCase):
    def parse(self, markup):
        with patch("builtins.open", mock_open(read_data=RATE_NOTE + markup)):
            return rc.displayed_state("memory.html")

    def test_exact_hcar_kpi_beats_many_community_and_mihu_amounts(self):
        shown = self.parse(COMPETING + HCAR_KPI + COMPETING)
        self.assertEqual(shown, state())

    def test_explicit_hcar_kpi_takes_precedence_over_legacy_label(self):
        self.assertEqual(self.parse(LEGACY_KPI + HCAR_KPI)["price"], 639000)

    def test_scoped_legacy_kpi_has_unknown_period_and_legacy_definition(self):
        shown = self.parse(COMPETING + LEGACY_KPI + COMPETING)
        self.assertEqual(shown, state(legacy=True))

    def test_legacy_scoping_does_not_follow_value_into_another_kpi(self):
        malformed = ('<div class="kpi"><div class="label">'
                     'For Sale: Median Listing Price</div></div>' + COMPETING)
        with self.assertRaisesRegex(ValueError, "scoped value sibling"):
            self.parse(malformed)

    def test_legacy_label_requires_exact_text(self):
        for text in ("Community For Sale: Median Listing Price",
                     "For Sale: Median Listing Price Estimate", "Median Listing Price"):
            with self.subTest(label=text), self.assertRaises(ValueError):
                self.parse(LEGACY_KPI.replace("For Sale: Median Listing Price", text))

    def test_legacy_label_must_be_within_kpi(self):
        with self.assertRaises(ValueError):
            self.parse(LEGACY_KPI.replace('class="kpi"', 'class="community"'))

    def test_missing_county_kpi_never_falls_back_to_frequent_amount(self):
        with self.assertRaises(ValueError):
            self.parse(COMPETING)

    def test_duplicate_hcar_or_legacy_kpis_are_rejected(self):
        for markup in (HCAR_KPI * 2, LEGACY_KPI * 2):
            with self.subTest(markup=markup), self.assertRaises(ValueError):
                self.parse(markup)

    def test_bad_hcar_source_does_not_fall_back_to_legacy(self):
        with self.assertRaisesRegex(ValueError, "source markup"):
            self.parse(HCAR_KPI.replace('data-source="hcar"', 'data-source="realtor"')
                       + LEGACY_KPI)

    def test_missing_or_invalid_hcar_period_is_rejected(self):
        for markup in (HCAR_KPI.replace(' data-period="2026-08"', ""),
                       HCAR_KPI.replace("2026-08", "2026-09"),
                       HCAR_KPI.replace("2026-08", "2026-8")):
            with self.subTest(markup=markup), self.assertRaises(ValueError):
                self.parse(markup)

    def test_kpi_must_contain_exactly_one_dollar_amount(self):
        for amount in ("$639,000 $657,475", "$639,000 estimated", "$0", "$63,90"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.parse(HCAR_KPI.replace("$639,000", amount))

    def test_compatible_displayed_values_tuple(self):
        with patch("builtins.open", mock_open(read_data=RATE_NOTE + HCAR_KPI)):
            self.assertEqual(rc.displayed_values("memory.html"),
                             (6.66, "July 30, 2026", 639000.0))


class EvidenceTests(OfflineTestCase):
    def validate(self, supplied=None, price=639000, period="2026-08"):
        return rc.validate_hcar_evidence(
            evidence() if supplied is None else supplied, price, period)

    def test_exact_evidence_is_accepted_and_preserved(self):
        supplied = evidence(verification_notes="Independent PDF table extraction")
        result = self.validate(supplied)
        self.assertEqual(result, supplied)
        self.assertIsNot(result, supplied)

    def test_numeric_float_price_is_accepted_if_exact(self):
        self.validate(evidence(price=639000.0, verification_price=639000.0), 639000.0)

    def test_every_required_field_is_mandatory(self):
        for key in evidence():
            supplied = evidence()
            del supplied[key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.validate(supplied)

    def test_evidence_must_be_an_object(self):
        for supplied in ([], "", 639000, True):
            with self.subTest(supplied=supplied), self.assertRaises(ValueError):
                self.validate(supplied)

    def test_source_metric_and_geography_are_exact(self):
        bad_metadata = {
            "source": ["Realtor.com", "HCAR", "HCAR / Bright MLS "],
            "metric": ["median_listing_price", "average_sold_price", "Median Sold Price"],
            "geography": ["Howard County", "Howard County, MD", "Howard County, Indiana"],
        }
        for key, values in bad_metadata.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.validate(evidence(**{key: value}))

    def test_price_and_verification_price_must_match_cli_exactly(self):
        for key in ("price", "verification_price"):
            for value in (639001, 638999, 639000.001, "639000", True, None):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.validate(evidence(**{key: value}))

    def test_nonpositive_or_nonfinite_prices_are_rejected(self):
        for value in (0, -639000, float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.validate(evidence(price=value, verification_price=value), value)

    def test_verification_period_must_match(self):
        with self.assertRaisesRegex(ValueError, "periods must match"):
            self.validate(evidence(verification_period="2026-07"))

    def test_older_period_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "older than"):
            self.validate(evidence(period="2026-07", verification_period="2026-07"))

    def test_current_and_future_reporting_months_are_rejected(self):
        for period in ("2026-09", "2026-10", "2027-01"):
            with self.subTest(period=period), self.assertRaisesRegex(ValueError, "completed"):
                self.validate(evidence(period=period, verification_period=period))

    def test_invalid_period_formats_are_rejected(self):
        for period in ("2026-8", "202608", "2026-00", "2026-13", "0000-08",
                       "2026-08-31", "", None, 202608):
            with self.subTest(period=period), self.assertRaises(ValueError):
                self.validate(evidence(period=period, verification_period=period))

    def test_completed_month_across_year_boundary(self):
        rc.validate_hcar_evidence(
            evidence(period="2025-12", verification_period="2025-12"),
            639000, "2025-11", today=date(2026, 1, 1))

    def test_period_advance_is_valid_and_legacy_period_can_be_unknown(self):
        self.validate(period="2026-07")
        self.validate(period=None)

    def test_reports_must_be_different(self):
        with self.assertRaisesRegex(ValueError, "must be different"):
            self.validate(evidence(verification_url=DETAILED))

    def test_bad_report_hosts_protocols_and_nonexact_urls_are_rejected(self):
        urls = [
            DETAILED.replace("https:", "http:"),
            DETAILED.replace("www.hcar.org", "hcar.org.evil.example"),
            DETAILED.replace("www.hcar.org", "evil.example"),
            DETAILED.replace("www.hcar.org", "evil.example@hcar.org"),
            DETAILED.replace("www.hcar.org", "hcar.org@evil.example"),
            DETAILED.replace("www.hcar.org", "www.hcar.org:444"),
            DETAILED + "?download=1", DETAILED + "#page=1",
            "https://www.hcar.org/market-statistics/",
            DETAILED.replace(".pdf", ".html"),
            DETAILED + ".exe", DETAILED + " ", "", None,
            DETAILED.replace("Detailed_Report", "Average_Report"),
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.validate(evidence(report_url=url))

    def test_insight_url_has_equally_strict_validation(self):
        for url in (INSIGHT.replace("https:", "http:"), INSIGHT + "?download=1",
                    INSIGHT.replace("www.hcar.org", "fake.hcar.org"),
                    INSIGHT.replace("Insight_Report", "Detailed_Report")):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.validate(evidence(verification_url=url))

    def test_report_types_cannot_be_swapped(self):
        with self.assertRaises(ValueError):
            self.validate(evidence(report_url=INSIGHT, verification_url=DETAILED))

    def test_bare_hcar_host_and_encoded_report_suffix_are_valid(self):
        self.validate(evidence(
            report_url=DETAILED.replace("www.hcar.org", "hcar.org")
            .replace("(1)", "%281%29")))

    def test_detailed_market_report_filename_variant_is_valid(self):
        self.validate(evidence(report_url=REPORT_ROOT + "July_2026_Detailed_Market_Report.pdf"))

    def test_historic_short_detailed_and_insight_filenames_are_valid(self):
        self.validate(evidence(report_url=REPORT_ROOT + "Detailed-Apr26.pdf",
                               verification_url=REPORT_ROOT + "Insight-May26.pdf"))

    def test_report_role_and_pdf_suffix_are_case_insensitive(self):
        self.validate(evidence(report_url=REPORT_ROOT + "DETAILED-APR26.PDF",
                               verification_url=REPORT_ROOT + "INSIGHT-MAY26.PdF"))


class DecisionTests(OfflineTestCase):
    def run_check(self, args=(), shown=None, supplied=None, rate=6.48, history=None,
                  raw_evidence=None):
        shown = shown or state()
        supplied = evidence() if supplied is None else supplied
        raw = json.dumps(supplied) if raw_evidence is None else raw_evidence
        captured = []
        history = [] if history is None else history
        output = io.StringIO()

        def capture(entry, no_record):
            self.assertTrue(no_record)
            captured.append(entry)
            return copy.deepcopy(history)

        with patch.object(rc, "displayed_state", return_value=shown), \
                patch.object(rc, "latest_pmms", return_value=(rate, "September 10, 2026")) as pmms, \
                patch.object(rc, "record_entry", side_effect=capture), \
                patch("builtins.open", mock_open(read_data=raw)), redirect_stdout(output):
            code = rc.main(["--no-record", *args])
        return code, output.getvalue(), captured[0], pmms

    def price_args(self, price=639000):
        return ["--price", str(price), "--hcar-evidence", "memory-evidence.json"]

    def test_price_without_evidence_blocks_before_pmms(self):
        code, text, entry, pmms = self.run_check(["--price", "639000"])
        self.assertEqual(code, 3)
        self.assertEqual(entry["action"], "blocked")
        self.assertIn("BLOCKED", text)
        self.assertNotIn("--no-crosscheck", text)
        pmms.assert_not_called()

    def test_invalid_evidence_blocks_before_pmms_even_if_rate_would_cross(self):
        for supplied in (evidence(price=640000), evidence(source="Realtor.com"),
                         evidence(period="2026-09", verification_period="2026-09"),
                         evidence(period="2026-07", verification_period="2026-07"),
                         evidence(report_url="https://example.com/Detailed_Report.pdf")):
            with self.subTest(supplied=supplied):
                code, text, entry, pmms = self.run_check(
                    self.price_args(), supplied=supplied, rate=6.0)
                self.assertEqual(code, 3)
                self.assertEqual(entry["action"], "blocked")
                self.assertNotIn("DECISION: UPDATE", text)
                pmms.assert_not_called()

    def test_malformed_json_blocks(self):
        code, _, _, pmms = self.run_check(self.price_args(), raw_evidence="{invalid")
        self.assertEqual(code, 3)
        pmms.assert_not_called()

    def test_nonfinite_cli_price_blocks_without_storing_nan(self):
        code, _, entry, pmms = self.run_check(self.price_args("nan"))
        self.assertEqual(code, 3)
        self.assertIsNone(entry["observed_price"])
        self.assertFalse(entry["advance_updated_badge"])
        pmms.assert_not_called()

    def test_blocked_exit_survives_unreadable_history(self):
        with patch.object(rc, "displayed_state", return_value=state()), \
                patch.object(rc, "record_entry", side_effect=ValueError("Invalid history")), \
                patch.object(rc, "latest_pmms") as pmms, \
                redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            code = rc.main(["--price", "639000", "--no-record"])
        self.assertEqual(code, 3)
        self.assertIn("BLOCKED", output.getvalue())
        pmms.assert_not_called()

    def test_missing_evidence_file_blocks(self):
        with patch.object(rc, "read_hcar_evidence", side_effect=FileNotFoundError("missing")):
            code, _, _, pmms = self.run_check(self.price_args())
        self.assertEqual(code, 3)
        pmms.assert_not_called()

    def test_rate_only_legacy_page_blocks_migration(self):
        code, text, entry, pmms = self.run_check(shown=state(legacy=True), rate=6.0)
        self.assertEqual(code, 3)
        self.assertIn("Source migration is incomplete", text)
        self.assertEqual(entry["displayed_price_source"], "legacy_realtor_listing")
        pmms.assert_not_called()

    def test_rate_only_hcar_page_holds_under_threshold(self):
        code, _, entry, _ = self.run_check()
        self.assertEqual(code, 0)
        self.assertEqual(entry["effective_rate"], 6.66)
        self.assertEqual(entry["effective_price"], 639000)
        self.assertIsNone(entry["hcar_evidence"])

    def test_rate_only_hcar_page_updates_at_exact_quarter_point(self):
        for rate in (6.41, 6.91):
            with self.subTest(rate=rate):
                code, _, entry, _ = self.run_check(rate=rate)
                self.assertEqual(code, 10)
                self.assertTrue(entry["rate_crossed"])
                self.assertEqual(entry["effective_rate"], rate)

    def test_source_switch_is_definition_change_not_market_decrease(self):
        code, text, entry, _ = self.run_check(self.price_args(), shown=state(legacy=True))
        self.assertEqual(code, 10)
        self.assertTrue(entry["source_changed"])
        self.assertTrue(entry["numeric_price_changed"])
        self.assertFalse(entry["price_changed"])
        self.assertEqual(entry["change_type"], "definition/source change")
        self.assertEqual(entry["hcar_evidence"], evidence())
        self.assertEqual(entry["observed_price_period"], "2026-08")
        self.assertIn("not market movement", text)
        self.assertNotIn("price moved", text)
        self.assertNotIn("decrease", text.lower())
        self.assertEqual(entry["effective_rate"], 6.66)

    def test_source_switch_updates_even_with_identical_price_and_rate(self):
        code, text, entry, _ = self.run_check(
            self.price_args(), shown=state(legacy=True, price=639000), rate=6.66)
        self.assertEqual(code, 10)
        self.assertTrue(entry["source_changed"])
        self.assertFalse(entry["numeric_price_changed"])
        self.assertFalse(entry["price_changed"])
        self.assertTrue(entry["advance_updated_badge"])
        self.assertIn("definition/source change", text)

    def test_price_only_update_keeps_old_rate_and_prints_all_figures(self):
        with patch.object(rc, "monthly_figures", wraps=rc.monthly_figures) as figures:
            code, text, entry, _ = self.run_check(
                self.price_args(), shown=state(price=650000), rate=6.48)
        self.assertEqual(code, 10)
        self.assertTrue(entry["price_changed"])
        self.assertFalse(entry["source_changed"])
        self.assertEqual(entry["effective_rate"], 6.66)
        self.assertEqual(entry["effective_price"], 639000)
        self.assertEqual(figures.call_args_list[-1].args, (639000.0, 6.66))
        self.assertIn("OLD displayed rate of 6.66%", text)
        for label in ("Loan", "P&I", "Property tax", "Insurance", "PMI", "Total monthly",
                      "Income required", "Down payment", "Closing costs", "Cash to close"):
            self.assertIn(label, text)
        self.assertIn("price moved $-11,000", text)

    def test_period_only_advance_holds_and_does_not_advance_badge(self):
        for rate in (6.66, 6.48):
            with self.subTest(rate=rate):
                code, text, entry, _ = self.run_check(
                    self.price_args(), shown=state(period="2026-07"), rate=rate)
                self.assertEqual(code, 0)
                self.assertEqual(entry["action"], "hold")
                self.assertTrue(entry["metadata_refresh_only"])
                self.assertFalse(entry["advance_updated_badge"])
                self.assertEqual(entry["hcar_evidence"], evidence())
                self.assertIn("do not advance the Updated badge", text)

    def test_unchanged_price_and_period_hold(self):
        code, _, entry, _ = self.run_check(self.price_args())
        self.assertEqual(code, 0)
        self.assertFalse(entry["period_advanced"])
        self.assertFalse(entry["advance_updated_badge"])

    def test_quarter_point_accumulates_against_display_not_previous_reading(self):
        history = []
        for rate, expected in ((6.56, 0), (6.48, 0), (6.42, 0), (6.41, 10)):
            with self.subTest(rate=rate):
                code, _, entry, _ = self.run_check(rate=rate, history=history)
                self.assertEqual(code, expected)
                self.assertEqual(entry["displayed_rate"], 6.66)
                self.assertEqual(entry["gap"], round(rate - 6.66, 4))
                self.assertEqual(entry["effective_rate"], rate if expected == 10 else 6.66)
                history.append(entry)

    def test_existing_four_decimal_gap_rounding_is_unchanged(self):
        code, _, entry, _ = self.run_check(rate=6.41004)
        self.assertEqual(code, 10)
        self.assertEqual(entry["gap"], -0.25)

    def test_custom_threshold_is_preserved(self):
        code, _, entry, _ = self.run_check(["--threshold", "0.20"], rate=6.46)
        self.assertEqual(code, 10)
        self.assertEqual(entry["threshold"], 0.20)

    def test_existing_consecutive_hold_note_is_preserved(self):
        history = [{"action": "hold", "gap": -0.16}, {"action": "hold", "gap": -0.17}]
        code, text, _, _ = self.run_check(history=history)
        self.assertEqual(code, 0)
        self.assertIn("3 consecutive checks", text)

    def test_evidence_without_price_is_blocked(self):
        code, _, _, pmms = self.run_check(["--hcar-evidence", "memory-evidence.json"])
        self.assertEqual(code, 3)
        pmms.assert_not_called()

    def test_no_crosscheck_bypass_and_old_tolerance_flags_are_removed(self):
        for option in (["--no-crosscheck"], ["--tolerance", "10"]):
            with self.subTest(option=option), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    rc.main(option)
                self.assertEqual(exc.exception.code, 2)


class HistoryTests(OfflineTestCase):
    def test_new_entry_preserves_all_prior_records_and_evidence(self):
        prior = [
            {"action": "update", "crosscheck": {"reference": 657475}, "unknown_old_key": 7},
            {"action": "hold", "gap": -0.18},
        ]
        original = copy.deepcopy(prior)
        entry = {"action": "update", "hcar_evidence": evidence(),
                 "change_type": "definition/source change"}
        opened = mock_open()
        with patch.object(rc, "load_history", return_value=prior), \
                patch("builtins.open", opened):
            result = rc.record_entry(entry, no_record=False)
        text = "".join(call.args[0] for call in opened.return_value.write.call_args_list)
        stored = json.loads(text)
        self.assertEqual(prior, original)
        self.assertEqual(result, original)
        self.assertEqual(stored[:-1], original)
        self.assertEqual(stored[-1], entry)
        opened.assert_called_once_with(rc.HISTORY, "w", encoding="utf-8")

    def test_no_record_never_opens_history_for_writing(self):
        with patch.object(rc, "load_history", return_value=[]), \
                patch("builtins.open") as opened:
            self.assertEqual(rc.record_entry({"action": "hold"}, no_record=True), [])
        opened.assert_not_called()

    def test_corrupt_history_is_not_silently_discarded_or_overwritten(self):
        for raw in ("{invalid", "{}", "[1]"):
            with self.subTest(raw=raw), \
                    patch.object(rc.os.path, "exists", return_value=True), \
                    patch("builtins.open", mock_open(read_data=raw)) as opened:
                with self.assertRaises(ValueError):
                    rc.record_entry({"action": "update"}, no_record=False)
                opened.assert_called_once_with(rc.HISTORY, encoding="utf-8")

    def test_missing_history_starts_empty(self):
        with patch.object(rc.os.path, "exists", return_value=False):
            self.assertEqual(rc.load_history(), [])


class IntegrationTests(OfflineTestCase):
    def run_virtual_dashboard(self, markup, args):
        documents = {
            "memory.html": RATE_NOTE + COMPETING + markup + COMPETING,
            "evidence.json": json.dumps(evidence()),
        }

        def virtual_open(path, mode="r", **kwargs):
            self.assertEqual(mode, "r", "No actual files or history may be changed.")
            return io.StringIO(documents[path])

        output = io.StringIO()
        with patch("builtins.open", side_effect=virtual_open), \
                patch.object(rc, "latest_pmms", return_value=(6.48, "September 10, 2026")), \
                patch.object(rc, "load_history", return_value=[]), redirect_stdout(output):
            code = rc.main(["--html", "memory.html", "--no-record", *args])
        self.network.assert_not_called()
        return code, output.getvalue()

    def test_actual_cli_path_migrates_scoped_legacy_kpi_with_evidence(self):
        code, text = self.run_virtual_dashboard(
            LEGACY_KPI, ["--price", "639000", "--hcar-evidence", "evidence.json"])
        self.assertEqual(code, 10)
        self.assertIn("definition/source change", text)
        self.assertIn("199,300", text)
        self.assertIn("19,170", text)
        self.assertIn("OLD displayed rate of 6.66%", text)

    def test_actual_cli_path_uses_explicit_hcar_kpi_for_rate_only_hold(self):
        code, text = self.run_virtual_dashboard(HCAR_KPI, [])
        self.assertEqual(code, 0)
        self.assertIn("Displayed median price : $639,000", text)
        self.assertIn("HCAR / Bright MLS (2026-08)", text)


class FormulaTests(OfflineTestCase):
    def test_original_legacy_formula_regression_fixture(self):
        self.assertEqual(rc.monthly_figures(657475, 6.66), {
            "loan": 591728, "pi": 3803, "tax": 556, "insurance": 150, "pmi": 271,
            "total_monthly": 4780, "income_required": 204850, "down_payment": 65748,
            "closing_costs": 19724, "cash_to_close": 85472,
        })

    def test_hcar_formula_at_retained_displayed_rate(self):
        self.assertEqual(rc.monthly_figures(639000, 6.66), {
            "loan": 575100, "pi": 3696, "tax": 540, "insurance": 150, "pmi": 264,
            "total_monthly": 4650, "income_required": 199300, "down_payment": 63900,
            "closing_costs": 19170, "cash_to_close": 83070,
        })

    def test_hcar_formula_at_different_rate(self):
        self.assertEqual(rc.monthly_figures(639000, 6.48), {
            "loan": 575100, "pi": 3627, "tax": 540, "insurance": 150, "pmi": 264,
            "total_monthly": 4581, "income_required": 196350, "down_payment": 63900,
            "closing_costs": 19170, "cash_to_close": 83070,
        })

    def test_zero_rate_formula_remains_supported(self):
        self.assertEqual(rc.monthly_figures(639000, 0), {
            "loan": 575100, "pi": 1598, "tax": 540, "insurance": 150, "pmi": 264,
            "total_monthly": 2552, "income_required": 109350, "down_payment": 63900,
            "closing_costs": 19170, "cash_to_close": 83070,
        })

    def test_cash_to_close_keeps_rounding_combined_unrounded_inputs(self):
        figures = rc.monthly_figures(657450, 6.66)
        self.assertEqual(figures["closing_costs"], 19724)
        self.assertEqual(figures["down_payment"], 65745)
        self.assertEqual(figures["cash_to_close"], 85468)
        # Deliberately NOT down_payment + closing_costs, which would change rounding.
        self.assertNotEqual(figures["cash_to_close"],
                            figures["down_payment"] + figures["closing_costs"])

    def test_monthly_total_is_sum_of_rounded_components(self):
        for price in (639000, 657475, 657450, 650001):
            for rate in (0, 6.41, 6.48, 6.66, 6.91):
                with self.subTest(price=price, rate=rate):
                    figures = rc.monthly_figures(price, rate)
                    self.assertEqual(figures["total_monthly"],
                                     sum(figures[k] for k in ("pi", "tax", "insurance", "pmi")))
                    self.assertEqual(figures["income_required"] % 50, 0)


if __name__ == "__main__":
    unittest.main()
