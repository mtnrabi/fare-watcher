"""No network and no key: the saved responses in recorded/ answer every search.

    python -m unittest discover -s tests
"""
import contextlib
import csv
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fare_watch as fw  # noqa: E402

TODAY = date(2026, 11, 25)
WATCH = json.loads((ROOT / "watch.json").read_text())


def config(**changes):
    return fw.load_config({**WATCH, **changes}, TODAY)


def run(*args, env=None):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env or {}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        for name in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY", "RAPIDAPI_KEY"):
            if name not in (env or {}):
                os.environ.pop(name, None)
        code = fw.main(list(args))
    return code, out.getvalue(), err.getvalue()


def fare(price=300, calendar=300, reasons=("Google marks it low",), depart="2027-01-12", status="ok"):
    return fw.Fare("JFK", "LIS", 7, depart, "2027-01-19", calendar, status, price, "low", 420, 700,
                   "TAP", 0, "https://example.com", list(reasons))


class ConfigTest(unittest.TestCase):
    def test_shipped_config(self):
        c = config()
        self.assertEqual(len(c.routes), 8)
        self.assertEqual((c.date_from, c.date_to, c.days), (date(2027, 1, 9), date(2027, 2, 8), 31))
        self.assertEqual(c.requests_per_run, 10)

    def test_plan_text(self):
        text = fw.plan_text(config())
        self.assertIn("8 calendar request(s) + 2 confirmation(s) = 10 requests a run", text)
        self.assertIn("300 requests a month", text)
        self.assertIn("50 requests a month: 5 run(s)", text)
        self.assertIn("2,500 requests a month: 250 run(s)", text)

    def test_bad_configs_stop_before_any_request(self):
        for bad in ({"from": ["NEWYORK"]}, {"to": []}, {"nights": [0]}, {"nights": [31]}, {"nights": "7"},
                    {"adults": 0}, {"window": {"starts_in_days": 45, "days": 62}}, {"target_price": "cheap"},
                    {"window": {"from": "2026-01-01", "to": "2026-01-31"}}, {"window": {"from": "soon"}},
                    {"max_requests": 5}, {"nigths": [7]}, {"from": ["JFK"], "to": ["JFK"]}):
            with self.subTest(bad), self.assertRaises(fw.Stop):
                config(**bad)
        with self.assertRaises(fw.Stop):
            fw.load_config([], TODAY)

    def test_fixed_window_that_already_started_begins_today(self):
        c = config(window={"from": "2026-11-01", "to": "2026-12-10"})
        self.assertEqual(c.date_from, TODAY)

    def test_same_airport_on_both_sides_is_skipped(self):
        c = config(**{"from": ["JFK", "LIS"], "to": ["LIS", "DUB"], "nights": [7]})
        self.assertEqual(c.routes, [("JFK", "LIS", 7), ("JFK", "DUB", 7), ("LIS", "DUB", 7)])


class ClientTest(unittest.TestCase):
    def test_one_retry_then_failed(self):
        calls = []

        def post(path, body):
            calls.append(path)
            raise fw.SearchFailed("HTTP 503")
        client = fw.Client(post, 10, 0)
        with self.assertRaises(fw.SearchFailed):
            client.call("/x", {})
        self.assertEqual((len(calls), client.sent), (2, 2))

    def test_budget_is_never_passed(self):
        def post(path, body):
            raise fw.SearchFailed("HTTP 503")
        cfg = config()
        client = fw.Client(post, cfg.max_requests, 0)
        bests = fw.scan_calendars(cfg, client)
        fw.confirm(cfg, client, fw.pick(bests, cfg.confirm))
        self.assertLessEqual(client.sent, cfg.max_requests)
        self.assertEqual({b.status for b in bests}, {"failed"})

    def test_a_retry_never_eats_a_later_request(self):
        seen = []

        def post(path, body):
            seen.append((body["from_airport"], body["to_airport"], body["nights"]))
            raise fw.SearchFailed("HTTP 503")
        cfg = config(max_requests=10)  # exactly one request each, no room for a retry
        fw.scan_calendars(cfg, fw.Client(post, 10, 0))
        self.assertEqual(seen, cfg.routes)

    def test_request_bodies(self):
        bodies = []

        def post(path, body):
            bodies.append((path, body))
            return fw.replay_post(ROOT / "recorded")(path, body)
        cfg = config()
        client = fw.Client(post, 12, 0)
        fw.confirm(cfg, client, fw.pick(fw.scan_calendars(cfg, client), 2))
        calendar, roundtrip = bodies[0], bodies[-1]
        self.assertEqual(calendar[0], "/api/google_flights/cheapest_dates/v1")
        self.assertEqual(calendar[1]["departure_date_from"], "2027-01-09")
        self.assertEqual(roundtrip[0], "/api/google_flights/roundtrip/v1")
        self.assertNotIn("sort_type", roundtrip[1])  # sorted by price, Google's verdict is not returned
        self.assertNotIn("max_price", roundtrip[1])

    def test_http_errors(self):
        def refuse(code):
            return mock.patch("urllib.request.urlopen", side_effect=fw.urllib.error.HTTPError(
                "https://x", code, "no", {}, io.BytesIO(b'{"detail": "x"}')))
        for code, kind in ((503, fw.SearchFailed), (401, fw.Stop), (403, fw.Stop), (422, fw.Stop), (429, fw.Stop)):
            with self.subTest(code), refuse(code), self.assertRaises(kind) as caught:
                fw.http_post("SECRET-KEY")("/x", {})
            self.assertNotIn("SECRET-KEY", str(caught.exception))


class ScanTest(unittest.TestCase):
    def test_calendar_best_date(self):
        best = fw.read_calendar({"dates": [
            {"departure_date": "2027-01-02", "return_date": "2027-01-09", "price": 500},
            {"departure_date": "2027-01-01", "return_date": "2027-01-08", "price": None},
            {"departure_date": "2027-01-03", "return_date": "2027-01-10", "price": 450}]}, "JFK", "LIS", 7)
        self.assertEqual((best.depart, best.price, best.highest, best.dates_priced), ("2027-01-03", 450, 500, 2))

    def test_calendar_with_no_fares(self):
        for answer in ({"dates": []}, {"dates": [{"price": None}]}, {}, []):
            self.assertEqual(fw.read_calendar(answer, "JFK", "LIS", 7).status, "no_fares")

    def test_pick_takes_turns_between_destinations(self):
        def best(dest, price):
            return fw.Best("JFK", dest, 7, "ok", "2027-01-01", "2027-01-08", price, price, 1)
        bests = [best("DUB", 1), best("DUB", 2), best("DUB", 3), best("LIS", 9), fw.Best("JFK", "X", 7, "failed")]
        self.assertEqual([(b.destination, b.price) for b in fw.pick(bests, 3)], [("DUB", 1), ("LIS", 9), ("DUB", 2)])
        self.assertEqual(len(fw.pick(bests, 99)), 4)
        self.assertEqual(fw.pick(bests, 0), [])

    def confirm_with(self, rows, **changes):
        cfg = config(**changes)
        best = fw.Best("JFK", "LIS", 7, "ok", "2027-01-12", "2027-01-19", 400, 700, 31)
        return fw.confirm(cfg, fw.Client(lambda p, b: rows, 5, 0), [best])[0]

    def row(self, price, verdict="typical", low=420, high=700):
        return {"total_price_as_number": price, "price_range_in_relation_to_other_periods": verdict,
                "price_insights_low": low, "price_insights_high": high, "departure_flight_airline": "easy | Jet",
                "total_stops": 0, "buy_link": "https://example.com"}

    def test_google_low_is_a_reason(self):
        f = self.confirm_with([self.row(450, "low"), self.row(440, "low")])
        self.assertEqual((f.price, f.airline, f.reasons), (440, "easyJet", ["Google marks it low"]))

    def test_calendar_price_under_the_usual_range_is_a_reason(self):
        self.assertEqual(self.confirm_with([self.row(450)]).reasons, ["it is under Google's usual range"])

    def test_target_price_is_a_reason(self):
        self.assertEqual(self.confirm_with([self.row(450, low=300)], target_price=400).reasons,
                         ["it is at or under your target of 400"])

    def test_google_rules_can_be_switched_off(self):
        self.assertEqual(self.confirm_with([self.row(450, "low")], alert_on_google_low=False).reasons, [])

    def test_no_verdict_is_not_low(self):
        f = self.confirm_with([self.row(450, None, None, None)])
        self.assertEqual((f.status, f.reasons), ("ok", []))

    def test_empty_search(self):
        self.assertEqual(self.confirm_with([]).status, "no_fares")


class AlertTest(unittest.TestCase):
    def test_once_per_price(self):
        alerts, state = fw.new_alerts([fare(300)], {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(fw.new_alerts([fare(300)], state)[0], [])
        self.assertEqual(len(fw.new_alerts([fare(290, 290)], state)[0]), 1)

    def test_forgets_a_deal_that_ended(self):
        _, state = fw.new_alerts([fare(300)], {})
        _, state = fw.new_alerts([fare(500, 500, reasons=())], state)
        self.assertEqual(state, {})

    def test_a_failed_search_keeps_the_memory(self):
        _, state = fw.new_alerts([fare(300)], {})
        alerts, after = fw.new_alerts([fare(status="failed")], state)
        self.assertEqual((alerts, after), ([], state))

    def test_another_month_is_another_deal(self):
        _, state = fw.new_alerts([fare(300)], {})
        self.assertEqual(len(fw.new_alerts([fare(350, 350, depart="2027-02-12")], state)[0]), 1)

    def test_text(self):
        title, body = fw.alert_text(config(), [fare(310, 300)], "2026-11-25 11:23 UTC")
        self.assertEqual(title, "JFK to LIS 300 USD, 7 nights")
        self.assertIn("Why: Google marks it low", body)
        self.assertIn("2026-11-25 11:23 UTC", body)


class RunTest(unittest.TestCase):
    def test_recorded_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run("--config", str(ROOT / "watch.json"), "--replay", str(ROOT / "recorded"), "--data", tmp)
            self.assertEqual(code, 0)
            self.assertIn("248 dated fares read, 10 requests", out)
            self.assertIn("| JFK → DUB | 7 | 2027-01-26 | 2027-02-02 | 394 | 469 |", out)
            self.assertIn("No alert", out)
            with (Path(tmp) / "history.csv").open() as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 8)
            self.assertEqual([r["google_says"] for r in rows if r["google_says"]], ["typical", "typical"])

    def test_alert_reaches_github_outputs_and_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "watch.json"
            cfg.write_text(json.dumps({**WATCH, "target_price": 400}))
            args = ("--config", str(cfg), "--replay", str(ROOT / "recorded"), "--data", str(Path(tmp) / "data"))
            env = {"GITHUB_OUTPUT": str(Path(tmp) / "out"), "GITHUB_STEP_SUMMARY": str(Path(tmp) / "summary")}
            here = os.getcwd()
            os.chdir(tmp)
            try:
                code, out, _ = run(*args, env=env)
                self.assertEqual(code, 0)
                self.assertIn("ALERT: JFK to DUB 394 USD, 7 nights", out)
                self.assertIn("alert=true\ntitle=JFK to DUB 394 USD, 7 nights\n", (Path(tmp) / "out").read_text())
                self.assertIn("at or under your target of 400", (Path(tmp) / "alert.md").read_text())
                self.assertIn("Fare watch", (Path(tmp) / "summary").read_text())
                self.assertIn("No alert", run(*args)[1])
            finally:
                os.chdir(here)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            run("--config", str(ROOT / "watch.json"), "--replay", str(ROOT / "recorded"), "--data", tmp + "/d", "--dry-run")
            self.assertFalse(Path(tmp, "d").exists())

    def test_nothing_finished_is_an_error_not_an_empty_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = run("--config", str(ROOT / "watch.json"), "--replay", tmp, "--data", tmp + "/d")
            self.assertEqual(code, 1)
            self.assertIn("no search finished", err)

    def test_missing_key_and_missing_config(self):
        code, _, err = run("--config", str(ROOT / "watch.json"))
        self.assertEqual(code, 2)
        self.assertIn("Set RAPIDAPI_KEY", err)
        self.assertEqual(run("--config", "nope.json", "--plan")[0], 2)


if __name__ == "__main__":
    unittest.main()
