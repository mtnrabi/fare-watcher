# fare-watcher

Scan a whole month, several airports and several trip lengths once a day, and get a GitHub issue (GitHub emails
it to you) when **Google marks the fare low**. Runs free on GitHub Actions, with no server and nothing to install: one Python
file, standard library only, MIT.

The shipped example watches New York (JFK and EWR) to Lisbon and Dublin, 7 or 10 nights, over 31 departure dates.
That is **248 dated round-trip fares for 10 requests**.

## How it works

1. **Calendar.** One request per (origin, destination, trip length) returns Google Flights' lowest fare for every
   departure date of the window. A month of dates is one request, not 31.
2. **Confirm.** The cheapest candidates, one destination at a time so a cheap city can't hide the others, get one
   full round-trip search each. That answer carries the airline, the stops, a booking link and Google's verdict on
   the fare (`low`, `typical` or `high`) next to its usual range for that route and those dates.
3. **Tell you, once.** An issue opens when Google says `low`, when the fare is under Google's usual range, or when
   it is at or under your own target. You hear about the same trip again only if it gets cheaper.
4. **Remember.** Every run appends to `data/history.csv` in your repo, so you also build your own price history.

A search that did not finish is shown as "search did not finish", never as "no flights".

## Real output

A real run, fetched live on **2026-11-25 between 11:23 and 11:26 UTC** (the eight calendars were read in 17 seconds). The raw
responses are in [`recorded/`](recorded). Trimmed, booking links left out:

```text
2 origin(s) x 2 destination(s) x 2 trip length(s), departures 2027-01-09 to 2027-02-08:
248 dated fares read, 10 requests.

route       nights  cheapest departure  return      USD  dearest date in the window
JFK → DUB   7       2027-01-26          2027-02-02  394  469
JFK → DUB   10      2027-02-01          2027-02-11  394  465
EWR → DUB   7       2027-02-01          2027-02-08  394  465
EWR → DUB   10      2027-02-01          2027-02-11  394  465
JFK → LIS   10      2027-01-16          2027-01-26  527  715
EWR → LIS   10      2027-01-16          2027-01-26  527  684
JFK → LIS   7       2027-01-12          2027-01-19  555  697
EWR → LIS   7       2027-01-12          2027-01-19  555  704

Confirmed with a full search
route       dates                     calendar  full search  Google says  usual range  stops  airline (outbound)
JFK → DUB   2027-01-26 to 2027-02-02  394       459          typical      345-610      1      British Airways, Aer Lingus / American
JFK → LIS   2027-01-16 to 2027-01-26  527       527          typical      420-700      2      Icelandair
```

Google called both `typical`, so this run stayed quiet. With `"target_price": 400` in the config the same answers
open this issue:

```text
JFK to DUB 394 USD, 7 nights

JFK → DUB: 394 USD round trip
- 2027-01-26 to 2027-02-02 (7 nights)
- Lowest on Google's calendar: 394. Best in the full search: 459, British Airways, Aer Lingus | American, 1 stop(s) in total
- Google says typical, usual range 345-610 USD
- Why: it is at or under your target of 400
- Book the 459 fare: https://www.google.com/travel/flights?tfs=...
```

Why two prices: the calendar is the lowest fare of any itinerary that day, the full search ranks the best ones on price, time
and stops together. They matched for Lisbon above. For Dublin
the 394 is an Icelandair trip with 2 stops that only a price-sorted search lists, so you see both numbers.

Try it without a key, on the saved responses:

```bash
git clone https://github.com/mtnrabi/fare-watcher && cd fare-watcher
python fare_watch.py --replay recorded --dry-run
```

## Set it up (5 minutes)

1. Press **Use this template** (or copy these files into a new repo).
2. Get a key: subscribe to the free BASIC plan of the
   [Google Flights Live API on RapidAPI](https://rapidapi.com/mtnrabi/api/google-flights-live-api/pricing) (no card) and copy your
   `X-RapidAPI-Key`.
3. In your repo: **Settings > Secrets and variables > Actions > New repository secret**, name `RAPIDAPI_KEY`.
4. Edit `watch.json` with your airports.
5. **Actions > fare watch > Run workflow** once. After that it runs every day at 06:17 UTC (change the `cron` line in
   [`.github/workflows/fare-watch.yml`](.github/workflows/fare-watch.yml)).

The workflow uses the repo's own `GITHUB_TOKEN` to open the issue and commit `data/`. A public repo shows your
watch and its history to everyone, so make yours private if you care.

## watch.json

```json
{
  "from": ["JFK", "EWR"],
  "to": ["LIS", "DUB"],
  "nights": [7, 10],
  "window": { "starts_in_days": 45, "days": 31 },
  "currency": "USD",
  "confirm": 2,
  "max_requests": 12
}
```

| Setting | Meaning |
|---|---|
| `from`, `to` | 3-letter airport codes, as many as you like |
| `nights` | trip lengths, 1 to 30 |
| `window` | rolling: `{"starts_in_days": 45, "days": 31}`, or fixed: `{"from": "2027-03-01", "to": "2027-03-31"}`; at most 61 days |
| `confirm` | how many candidates get a full search (default 3) |
| `max_requests` | a run never sends more than this, retries included (default 20) |
| `target_price` | optional: also alert at or under this total |
| `alert_on_google_low` | `false` = only your target counts (default `true`) |
| `currency`, `adults` | default `USD`, 1 adult; prices are for all adults together |

The config is checked before anything is sent, so a typo costs no request.

## What it costs

One calendar is one request, one confirmation is one request. `python fare_watch.py --plan` prints the count for your
config without sending anything.

| Your watch | Dated fares per run | Requests per run | Fits |
|---|---|---|---|
| 1 route, 1 trip length, confirm 1 | 31 | 2 | free plan, every weekday (46 a month at most) |
| the example: 2 x 2 airports, 2 lengths, confirm 2 | 248 | 10 | free plan once a week (at most 5 runs, 50 requests); PRO every day (310 a month) |
| 3 x 8 airports, 3 lengths, confirm 8 | 2,232 | 80 | PRO, every day (2,480 in a 31-day month) |

Plans, read from the live listing on 2026-11-25: **free BASIC is 50 requests a month** (hard cap, no card), **PRO is $10
for 2,500** (then $0.003 each, 150 a minute). Answers of 500 and above don't count against your quota. The
[listing](https://rapidapi.com/mtnrabi/api/google-flights-live-api/pricing) is the authority if this README gets old.

## The two requests

```bash
# a month of fares for one route and trip length
curl -X POST https://google-flights-live-api.p.rapidapi.com/api/google_flights/cheapest_dates/v1 \
  -H "Content-Type: application/json" \
  -H "x-rapidapi-host: google-flights-live-api.p.rapidapi.com" \
  -H "x-rapidapi-key: $RAPIDAPI_KEY" \
  -d '{"from_airport":"JFK","to_airport":"DUB","departure_date_from":"2027-01-09","departure_date_to":"2027-02-08","nights":7,"currency":"USD"}'

# one date pair in full, with Google's verdict
curl -X POST https://google-flights-live-api.p.rapidapi.com/api/google_flights/roundtrip/v1 \
  -H "Content-Type: application/json" \
  -H "x-rapidapi-host: google-flights-live-api.p.rapidapi.com" \
  -H "x-rapidapi-key: $RAPIDAPI_KEY" \
  -d '{"from_airport":"JFK","to_airport":"DUB","departure_date":"2027-01-26","return_date":"2027-02-02","currency":"usd","limit":10}'
```

Leave `sort_type` out of the second one: Google publishes no verdict on price-sorted pages. Docs:
[cheapest dates](https://flightpowers.com/flights-api/cheapest-dates),
[round trip](https://flightpowers.com/flights-api/round-trip),
[price insights](https://flightpowers.com/flights-api/price-insights).

The script also sends `_fp_source` / `_fp_tool` so FlightPowers can count requests that come from this example. The API
removes them before the search, and you can delete the line.

## Tests

```bash
python -m unittest discover -s tests
```

No network and no key, they run on `recorded/`.

## Scope

Round trips, adults only (the calendar doesn't take children). It reports fares and links to Google Flights; it does not book.
Fares move during the day, so open the link before you decide.

Built by the maker of the API it uses, [FlightPowers](https://flightpowers.com). Not affiliated with Google. MIT licensed.
