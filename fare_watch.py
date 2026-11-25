#!/usr/bin/env python3
"""fare-watcher: scan a month x several airports x trip lengths once a day.

Two steps, so a wide scan stays cheap:

1. Calendar. ONE request per (origin, destination, trip length) returns Google
   Flights' lowest fare for every departure date of the window (up to 61 days).
2. Confirm. The best few candidates get one full round-trip search each. That
   answer has the airline, a booking link and Google's own verdict on the fare:
   low, typical or high for that route and period.

You get an alert when Google marks the fare low, when it is under Google's usual
range, or when it is at or under your own target price. Once per price.

Standard library only. Python 3.9+.

    export RAPIDAPI_KEY=your-key
    python fare_watch.py --plan     # how many requests a run costs (no network)
    python fare_watch.py            # run the scan
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from itertools import product
from pathlib import Path
from typing import Callable, Optional

API_HOST = "google-flights-live-api.p.rapidapi.com"
CALENDAR = "/api/google_flights/cheapest_dates/v1"
ROUNDTRIP = "/api/google_flights/roundtrip/v1"
LISTING = "https://rapidapi.com/mtnrabi/api/google-flights-live-api"
USER_AGENT = "fare-watcher/1.0 (+https://github.com/mtnrabi/fare-watcher)"

# Optional: lets FlightPowers count requests that come from this example. The
# API drops both fields before it reads the search. Delete them if you prefer.
ATTRIBUTION = {"_fp_source": "gh-example", "_fp_tool": "fare-watcher"}

Post = Callable[[str, dict], object]


class SearchFailed(Exception):
    """The search did not finish (5xx or network). Says nothing about fares."""


class Stop(Exception):
    """The run cannot go on (bad key, quota used, refused request, budget)."""


# ---------------------------------------------------------------- configuration


@dataclass
class Config:
    origins: list[str]
    destinations: list[str]
    nights: list[int]
    date_from: date
    date_to: date
    currency: str = "USD"
    adults: int = 1
    confirm: int = 3
    max_requests: int = 20
    alert_on_google_low: bool = True
    target_price: Optional[float] = None

    @property
    def routes(self) -> list[tuple[str, str, int]]:
        return [(o, d, n) for o, d, n in product(self.origins, self.destinations, self.nights) if o != d]

    @property
    def days(self) -> int:
        return (self.date_to - self.date_from).days + 1

    @property
    def requests_per_run(self) -> int:
        return len(self.routes) + min(self.confirm, len(self.routes))


def _airports(raw: dict, key: str) -> list[str]:
    value = raw.get(key)
    codes = [value] if isinstance(value, str) else list(value or [])
    codes = [str(c).strip().upper() for c in codes]
    bad = [c for c in codes if not re.fullmatch(r"[A-Z]{3}", c)]
    if not codes or bad:
        raise Stop(f'"{key}" needs 3-letter airport codes, e.g. ["JFK", "EWR"]' + (f", got {bad}" if bad else ""))
    return list(dict.fromkeys(codes))


def _whole(raw: dict, key: str, default: int, low: int, high: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise Stop(f'"{key}" must be a whole number from {low} to {high}, got {value!r}')
    return value


def load_config(raw: dict, today: date) -> Config:
    """Validate here, before any request: the API also counts a refused (4xx) request."""
    if not isinstance(raw, dict):
        raise Stop("the config must be a JSON object")
    known = {"from", "to", "nights", "window", "currency", "adults", "confirm", "max_requests",
             "alert_on_google_low", "target_price"}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise Stop(f"unknown setting(s) in the config: {', '.join(unknown)}")

    nights = raw.get("nights", [7])
    nights = [nights] if isinstance(nights, int) else nights
    if not isinstance(nights, list) or not nights or any(isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 30 for n in nights):
        raise Stop('"nights" must be whole numbers from 1 to 30, e.g. [7, 10]')

    window = raw.get("window") or {}
    try:
        if "from" in window or "to" in window:
            date_from, date_to = date.fromisoformat(window["from"]), date.fromisoformat(window["to"])
            date_from = max(date_from, today)  # a fixed window keeps working once it has started
        else:
            date_from = today + timedelta(days=_whole(window, "starts_in_days", 45, 1, 300))
            date_to = date_from + timedelta(days=_whole(window, "days", 31, 1, 61) - 1)
    except (KeyError, TypeError, ValueError):
        raise Stop('"window" is {"starts_in_days": 45, "days": 31} or {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}')
    if date_to < date_from:
        raise Stop("the window has already passed, or it ends before it starts")
    if (date_to - date_from).days + 1 > 61:
        raise Stop("the window is at most 61 days")

    target = raw.get("target_price")
    if target is not None and (isinstance(target, bool) or not isinstance(target, (int, float)) or target <= 0):
        raise Stop('"target_price" must be a number, or leave it out')

    cfg = Config(
        origins=_airports(raw, "from"),
        destinations=_airports(raw, "to"),
        nights=sorted(set(nights)),
        date_from=date_from,
        date_to=date_to,
        currency=str(raw.get("currency", "USD")).upper(),
        adults=_whole(raw, "adults", 1, 1, 9),
        confirm=_whole(raw, "confirm", 3, 0, 50),
        max_requests=_whole(raw, "max_requests", 20, 1, 1000),
        alert_on_google_low=bool(raw.get("alert_on_google_low", True)),
        target_price=target,
    )
    if not cfg.routes:
        raise Stop('"from" and "to" are the same airport')
    if cfg.requests_per_run > cfg.max_requests:
        raise Stop(f"this scan needs {cfg.requests_per_run} requests and max_requests is {cfg.max_requests}; "
                   "raise it or watch fewer airports / trip lengths")
    return cfg


# ------------------------------------------------------------------ transport


def http_post(api_key: str, timeout: float = 90) -> Post:
    def post(path: str, body: dict) -> object:
        req = urllib.request.Request(
            f"https://{API_HOST}{path}",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "x-rapidapi-key": api_key,
                     "x-rapidapi-host": API_HOST, "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code >= 500:
                raise SearchFailed(f"HTTP {e.code}") from None
            if e.code in (401, 403):
                raise Stop(f"The API refused the key (HTTP {e.code}). Is it subscribed to {LISTING} ?") from None
            if e.code == 429:
                raise Stop("Rate limit or monthly quota reached (HTTP 429). See your plan on RapidAPI.") from None
            raise Stop(f"Request refused (HTTP {e.code}): {detail}") from None
        except (OSError, ValueError) as e:  # network trouble, a timeout, or a body that is not JSON
            raise SearchFailed(str(e)) from None
    return post


def _file_name(path: str, body: dict) -> str:
    kind = "calendar" if path == CALENDAR else "roundtrip"
    if kind == "calendar":
        dates = f"{body['departure_date_from']}_{body['departure_date_to']}_{body['nights']}n"
    else:
        dates = f"{body['departure_date']}_{body['return_date']}"
    return f"{kind}_{body['from_airport']}-{body['to_airport']}_{dates}.json"


def replay_post(folder: Path) -> Post:
    """Answers from saved responses: no key, no network."""
    def post(path: str, body: dict) -> object:
        f = folder / _file_name(path, body)
        if not f.exists():
            raise SearchFailed(f"no saved response {f.name}")
        return json.loads(f.read_text())
    return post


def recording(post: Post, folder: Path) -> Post:
    def wrapped(path: str, body: dict) -> object:
        answer = post(path, body)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / _file_name(path, body)).write_text(json.dumps(answer, indent=1, ensure_ascii=False) + "\n")
        return answer
    return wrapped


class Client:
    """Counts every request it sends and never passes the budget, retries included."""

    def __init__(self, post: Post, budget: int, retry_delay: float = 3.0):
        self.post, self.budget, self.retry_delay, self.sent = post, budget, retry_delay, 0

    def call(self, path: str, body: dict, keep: int = 0) -> object:
        """One request, and one retry if it fails. `keep` = requests the retry must leave for later."""
        for attempt in (1, 2):
            if self.sent >= self.budget:
                raise SearchFailed("request budget used")
            self.sent += 1
            try:
                return self.post(path, {**body, **ATTRIBUTION})
            except SearchFailed:
                if attempt == 2 or self.budget - self.sent <= keep:
                    raise
                time.sleep(self.retry_delay)
        raise AssertionError("unreachable")


# ----------------------------------------------------------------------- scan


@dataclass
class Best:
    """The cheapest departure date of one (origin, destination, nights)."""
    origin: str
    destination: str
    nights: int
    status: str  # ok | no_fares | failed
    depart: str = ""
    ret: str = ""
    price: Optional[float] = None
    highest: Optional[float] = None
    dates_priced: int = 0


@dataclass
class Fare:
    """One confirmed fare, from a full round-trip search."""
    origin: str
    destination: str
    nights: int
    depart: str
    ret: str
    calendar_price: float
    status: str  # ok | no_fares | failed
    price: Optional[float] = None
    verdict: str = ""
    usual_low: Optional[float] = None
    usual_high: Optional[float] = None
    airline: str = ""
    stops: Optional[int] = None
    link: str = ""
    reasons: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """What an alert is remembered by: route, trip length and departure month."""
        return f"{self.origin}-{self.destination}-{self.nights}n-{self.depart[:7]}"

    @property
    def lowest(self) -> float:
        """The calendar covers every itinerary of the date; the full search ranks the best ones
        (price, time and stops together), so the calendar can be the lower of the two."""
        return min(self.calendar_price, self.price)


def read_calendar(answer: object, origin: str, destination: str, nights: int) -> Best:
    rows = answer.get("dates") if isinstance(answer, dict) else None
    priced = [r for r in rows or [] if isinstance(r.get("price"), (int, float))]
    if not priced:
        return Best(origin, destination, nights, "no_fares")
    best = min(priced, key=lambda r: (r["price"], r["departure_date"]))
    return Best(origin, destination, nights, "ok", best["departure_date"], best["return_date"], best["price"],
                max(r["price"] for r in priced), len(priced))


def scan_calendars(cfg: Config, client: Client) -> list[Best]:
    out = []
    for i, (o, d, n) in enumerate(cfg.routes):
        body = {"from_airport": o, "to_airport": d, "nights": n, "adults": cfg.adults, "currency": cfg.currency,
                "departure_date_from": cfg.date_from.isoformat(), "departure_date_to": cfg.date_to.isoformat()}
        still_needed = cfg.requests_per_run - i - 1
        try:
            out.append(read_calendar(client.call(CALENDAR, body, keep=still_needed), o, d, n))
        except SearchFailed:
            out.append(Best(o, d, n, "failed"))
    return out


def pick(bests: list[Best], how_many: int) -> list[Best]:
    """Cheapest first, one destination at a time, so a cheap city cannot hide the others."""
    by_destination: dict[str, list[Best]] = {}
    for b in sorted((b for b in bests if b.status == "ok"), key=lambda b: b.price):
        by_destination.setdefault(b.destination, []).append(b)
    queues, out = list(by_destination.values()), []
    while queues and len(out) < how_many:
        for q in queues:
            if q and len(out) < how_many:
                out.append(q.pop(0))
        queues = [q for q in queues if q]
    return out


def _airline(name: str) -> str:
    """Google sometimes splits one carrier in two: "easy | Jet" is easyJet."""
    for split, joined in (("easy | Jet", "easyJet"), ("Jet | Blue", "JetBlue"), ("West | Jet", "WestJet")):
        name = name.replace(split, joined)
    return name


def confirm(cfg: Config, client: Client, candidates: list[Best]) -> list[Fare]:
    out = []
    for i, b in enumerate(candidates):
        fare = Fare(b.origin, b.destination, b.nights, b.depart, b.ret, b.price, "failed")
        body = {"from_airport": b.origin, "to_airport": b.destination, "departure_date": b.depart,
                "return_date": b.ret, "currency": cfg.currency.lower(), "limit": 10}
        if cfg.adults > 1:
            body["passengers"] = [1] * cfg.adults  # one code per traveller, 1 = adult
        # No "sort_type": sorted by price, an answer can come back without Google's verdict.
        try:
            rows = client.call(ROUNDTRIP, body, keep=len(candidates) - i - 1)
            priced = [r for r in rows if isinstance(r, dict)
                      and isinstance(r.get("total_price_as_number"), (int, float))]
            fare.status = "ok" if priced else "no_fares"
        except SearchFailed:
            priced = []
        if priced:
            r = min(priced, key=lambda r: r["total_price_as_number"])
            fare.price = r["total_price_as_number"]
            fare.verdict = r.get("price_range_in_relation_to_other_periods") or ""
            fare.usual_low, fare.usual_high = r.get("price_insights_low"), r.get("price_insights_high")
            fare.airline, fare.stops = _airline(r.get("departure_flight_airline") or ""), r.get("total_stops")
            fare.link = r.get("buy_link") or ""
            if cfg.alert_on_google_low:
                if fare.verdict == "low":
                    fare.reasons.append("Google marks it low")
                elif fare.usual_low is not None and fare.lowest < fare.usual_low:
                    fare.reasons.append("it is under Google's usual range")
            if cfg.target_price is not None and fare.lowest <= cfg.target_price:
                fare.reasons.append(f"it is at or under your target of {money(cfg.target_price)}")
        out.append(fare)
    return out


def new_alerts(fares: list[Fare], state: dict) -> tuple[list[Fare], dict]:
    """Alert once per price: again only when that trip gets cheaper. A trip that stops
    qualifying is forgotten; one that was not checked today keeps its memory."""
    state, alerts = dict(state), []
    for f in fares:
        if f.status != "ok":
            continue
        if not f.reasons:
            state.pop(f.key, None)
        elif f.key not in state or f.lowest < state[f.key]:
            alerts.append(f)
            state[f.key] = f.lowest
    return alerts, state


# --------------------------------------------------------------------- output


def money(v: Optional[float]) -> str:
    if v is None:
        return "-"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"


def _usual(f: Fare) -> str:
    return "-" if f.usual_low is None or f.usual_high is None else f"{money(f.usual_low)}-{money(f.usual_high)}"


def report(cfg: Config, bests: list[Best], fares: list[Fare], sent: int, fetched: str) -> str:
    cur = cfg.currency
    fares_read = sum(b.dates_priced for b in bests)
    lines = [
        f"## Fare watch, {fetched}",
        "",
        f"{len(cfg.origins)} origin(s) x {len(cfg.destinations)} destination(s) x {len(cfg.nights)} trip length(s), "
        f"departures {cfg.date_from} to {cfg.date_to}: {fares_read} dated fares read, {sent} requests.",
        "",
        "### Cheapest date of every combination",
        "",
        f"| route | nights | cheapest departure | return | {cur} | dearest date in the window |",
        "|---|---|---|---|---|---|",
    ]
    order = {"ok": 0, "no_fares": 1, "failed": 2}
    for b in sorted(bests, key=lambda b: (order[b.status], b.price or 0)):
        route = f"{b.origin} → {b.destination}"
        if b.status == "ok":
            lines.append(f"| {route} | {b.nights} | {b.depart} | {b.ret} | {money(b.price)} | {money(b.highest)} |")
        else:
            label = "no fares shown" if b.status == "no_fares" else "search did not finish"
            lines.append(f"| {route} | {b.nights} | {label} | - | - | - |")
    if fares:
        lines += ["", "### Confirmed with a full search", "",
                  f"| route | dates | calendar {cur} | full search {cur} | Google says | usual range | stops "
                  "| airline (outbound) | book |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for f in fares:
            head = f"| {f.origin} → {f.destination} | {f.depart} to {f.ret} | {money(f.calendar_price)} "
            if f.status == "ok":
                airline = f.airline.replace("|", "/")
                link = f"[Google Flights]({f.link})" if f.link else "-"
                lines.append(f"{head}| {money(f.price)} | {f.verdict or '-'} | {_usual(f)} | "
                             f"{'-' if f.stops is None else f.stops} | {airline} | {link} |")
            else:
                label = "no flights found" if f.status == "no_fares" else "search did not finish"
                lines.append(f"{head}| {label} | - | - | - | - | - |")
    return "\n".join(lines) + "\n"


def alert_text(cfg: Config, alerts: list[Fare], fetched: str) -> tuple[str, str]:
    best = min(alerts, key=lambda f: f.lowest)
    title = f"{best.origin} to {best.destination} {money(best.lowest)} {cfg.currency}, {best.nights} nights"
    if len(alerts) > 1:
        title += f" (+{len(alerts) - 1} more)"
    parts = []
    for f in sorted(alerts, key=lambda f: f.lowest):
        stops = "" if f.stops is None else f", {f.stops} stop(s) in total"
        parts.append(
            f"**{f.origin} → {f.destination}: {money(f.lowest)} {cfg.currency} round trip**\n\n"
            f"- {f.depart} to {f.ret} ({f.nights} nights)\n"
            f"- Lowest on Google's calendar: {money(f.calendar_price)}. Best in the full search: {money(f.price)}, "
            f"{f.airline or 'airline not shown'}{stops}\n"
            f"- Google says {f.verdict or 'nothing about this one'}, usual range {_usual(f)} {cfg.currency}\n"
            f"- Why: {' and '.join(f.reasons)}\n"
            f"- Book the {money(f.price)} fare: {f.link or '-'}\n")
    return title, "\n".join(parts) + f"\nFetched {fetched}. Fares move during the day, check before you book.\n"


HISTORY_COLUMNS = ["fetched", "origin", "destination", "nights", "depart", "return", "currency", "calendar_price",
                   "full_search_price", "google_says", "usual_low", "usual_high"]


def append_history(path: Path, cfg: Config, bests: list[Best], fares: list[Fare], fetched: str) -> None:
    confirmed = {(f.origin, f.destination, f.nights): f for f in fares if f.status == "ok"}
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", newline="") as fh:
        w = csv.writer(fh)
        if is_new:
            w.writerow(HISTORY_COLUMNS)
        for b in bests:
            if b.status != "ok":
                continue
            f = confirmed.get((b.origin, b.destination, b.nights))
            w.writerow([fetched, b.origin, b.destination, b.nights, b.depart, b.ret, cfg.currency, b.price,
                        *((f.price, f.verdict, f.usual_low, f.usual_high) if f else ("", "", "", ""))])


def plan_text(cfg: Config) -> str:
    n, month = cfg.requests_per_run, cfg.requests_per_run * 30
    fares = len(cfg.routes) * cfg.days
    return "\n".join([
        f"{len(cfg.routes)} calendar request(s) + {n - len(cfg.routes)} confirmation(s) = {n} requests a run",
        f"({fares:,} dated fares read per run; a failed search is retried once, "
        f"and a run never sends more than {cfg.max_requests})",
        f"once a day, 30 days: {month:,} requests a month",
        f"free plan, 50 requests a month: {50 // n} run(s) of this size",
        f"PRO, $10 for 2,500 requests a month: {2500 // n} run(s) of this size",
    ])


# ----------------------------------------------------------------------- main


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Scan a month x airports x trip lengths; alert when Google marks a fare low.")
    p.add_argument("--config", default="watch.json")
    p.add_argument("--data", default="data", help="folder for state.json and history.csv (default: data)")
    p.add_argument("--plan", action="store_true", help="print how many requests a run costs, then stop")
    p.add_argument("--dry-run", action="store_true", help="do not write state or history")
    p.add_argument("--replay", help="answer from a folder of saved responses (no key needed)")
    p.add_argument("--record", help="save every raw response into this folder")
    a = p.parse_args(argv)

    try:
        now = datetime.now(timezone.utc)
        stamp = Path(a.replay or ".") / "RECORDED_AT.txt"
        if a.replay and stamp.exists():
            now = datetime.strptime(stamp.read_text().strip(), "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
        try:
            raw = json.loads(Path(a.config).read_text())
        except FileNotFoundError:
            raise Stop(f"no config file {a.config}")
        except ValueError as e:
            raise Stop(f"{a.config} is not valid JSON: {e}")
        cfg = load_config(raw, now.date())
        if a.plan:
            print(plan_text(cfg))
            return 0

        if a.replay:
            post, delay = replay_post(Path(a.replay)), 0.0
        else:
            key = os.environ.get("RAPIDAPI_KEY", "").strip()
            if not key:
                raise Stop(f"Set RAPIDAPI_KEY first. A free key: {LISTING}")
            post, delay = http_post(key), 3.0
        if a.record:
            post = recording(post, Path(a.record))
        client = Client(post, cfg.max_requests, delay)

        bests = scan_calendars(cfg, client)
        fares = confirm(cfg, client, pick(bests, cfg.confirm))
    except Stop as e:
        print(f"fare-watcher: {e}", file=sys.stderr)
        return 2

    fetched = now.strftime("%Y-%m-%d %H:%M UTC")
    data = Path(a.data)
    state_file = data / "state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    alerts, state = new_alerts(fares, state)

    text = report(cfg, bests, fares, client.sent, fetched)
    print(text)
    if not a.dry_run:
        append_history(data / "history.csv", cfg, bests, fares, fetched)
        state_file.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(text)

    if alerts:
        title, body = alert_text(cfg, alerts, fetched)
        print(f"ALERT: {title}\n\n{body}")
        if os.environ.get("GITHUB_OUTPUT"):
            Path("alert.md").write_text(body)
            with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
                fh.write(f"alert=true\ntitle={title}\n")
    else:
        print("No alert: nothing new that qualifies.")

    if all(b.status == "failed" for b in bests):
        print("fare-watcher: no search finished, try again later", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
