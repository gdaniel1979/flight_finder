# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the application

```bash
python3.9 main.py                              # Full search using config.yaml
python3.9 main.py --dry-run                    # Preview destinations/dates without API calls
python3.9 main.py --date 2026-05-15            # Search a single specific date
python3.9 main.py --destinations BCN,BGY,STN   # Search specific destinations only
python3.9 main.py --config custom_config.yaml  # Use a different config file
```

## Settings web app

```bash
streamlit run webapp.py --server.port 8503 --server.address 127.0.0.1   # local only
```

`webapp.py` is a Streamlit form over every key in `config.yaml`. The tabs follow one rule: **Keresés** holds conditions that apply to every airline, **Légitársaságok** holds everything airline-specific (one row per airline with the same three settings — enabled, own price limit, destinations — plus the request pacing of the two data sources; destinations are a multiselect labelled `NAP (Naples)` with a "Minden útvonal" option stored as `"all"`, Ryanair's choices come live from its route API, cached for a day), **Email** and **Rendszer** (logging only) the rest. Do not put an airline-specific setting on another tab.

`config_store.py` is shared by `main.py`, the web app and the tests: `load_config()` returns the config in one unified layout via `normalize()` (which also migrates the old `rate_limit`, `airlines.*.currency` and `airlines.wizzair.request_delay` keys), `validate()` (reuses `SearchConfig` plus IATA/email/airline checks), `describe_changes()` and an atomic `save_config()` that first copies the old file to `config.yaml.bak`.

- **There is no login** (the user's explicit choice on 2026-10-09: single user). Anyone who can reach the port can edit the settings, so two guards stay in place: the Brevo API key is never rendered (the field is always empty; leaving it empty keeps the stored key), and `validate()` only accepts log file paths that are relative and inside the project.
- **Saving rewrites `config.yaml` with PyYAML**, so hand-written comments in it are lost (the key descriptions stay in `config.yaml.example`). Keys the form does not know are preserved.
- Changes apply to the next run; nothing is restarted. The app never triggers a search.
- `deploy/flight-finder-web.service` is the systemd unit (port 8503); installing it needs sudo and is done by hand, like the cron job.

## Running tests

```bash
python -m pytest tests/test_models.py tests/test_filter_offline.py tests/test_google_flights_offline.py tests/test_webapp_offline.py -v   # Unit tests (no network calls)
python -m pytest tests/ -v                 # All tests (live tests hit real APIs)
python tests/test_models.py                # Run model tests directly
```

`tests/test_models.py` — offline unit tests for models and scraper instantiation.  
`tests/test_filter_offline.py` — offline tests for the fast search path (fake scraper, fake HTTP session).  
`tests/test_google_flights_offline.py` — offline tests for the Google Flights scrapers (fake result rows).  
`tests/test_webapp_offline.py` — offline tests for `config_store.py` and the Streamlit app (driven with `streamlit.testing.v1.AppTest`).  
`tests/test_ryanair_live.py` and `tests/test_filter_live.py` — make real network requests to Ryanair APIs.

## Architecture

`FlightFilter.find_trips` (`filter.py`) picks a search path per scraper:

**Fast path** (`FlightFilter._find_trips_fast`) — for scrapers with `supports_round_trip_search = True` (Ryanair). One `search_round_trips()` call per (outbound date, return date) pair returns the cheapest morning-out/evening-back pair for *every* destination at once (farfnd `roundTripFares`, `durationFrom = durationTo = nights`). A 30-day day-trip search is 30 requests (~25 s). Destination and `max_price` filtering happen client-side (`max_price` is also sent as `priceValueTo`). Failed requests are counted in `FlightFilter.failed_requests` / `total_requests`; `main.py` turns a non-zero count into a warning line in the log and the email so a blocked run doesn't look like "no results".

**Two-phase path** (`FlightFilter._find_trips_two_phase`) — fallback for scrapers without round-trip search; ~1 hour for all Ryanair routes, which is why Ryanair no longer uses it:

**Phase 1 – Pre-filter** (`FlightFilter._prefilter_candidates`): calls `get_cheapest_per_day()` once per route direction to get a set of dates that have any flights at all. Yields `(dest, D1, D2)` candidates where both directions have flights.

**Phase 2 – Detailed search** (`FlightFilter._search_destination_dates`): for each candidate, fetches morning outbound flights on D1 and evening return flights on D2, then builds all valid `DayTrip` pairs by Cartesian product, filtering by `max_price`.

Both paths share one code path for day trips and multi-day trips: a day trip is simply 0 nights (`FlightFilter._night_range`).

### Modules

| File | Responsibility |
|------|---------------|
| `main.py` | CLI entry point, config loading, result formatting, log prepend, email dispatch |
| `models.py` | Pydantic models: `Flight`, `DayTrip`, `SearchConfig`, `Airline` enum |
| `filter.py` | `FlightFilter` — fast and two-phase search paths, trip pairing |
| `scrapers/base_scraper.py` | `BaseScraper` abstract class; provides `search_outbound_flights` / `search_return_flights` with time filtering |
| `scrapers/ryanair_scraper.py` | `RyanairScraper` — direct farfnd API calls |
| `scrapers/google_flights_scraper.py` | `GoogleFlightsClient` + `GoogleFlightsScraper` — Wizz Air, easyJet, Eurowings, Jet2, Norwegian etc. via Google Flights (`fast-flights`) |
| `notifier.py` | `EmailNotifier` — Brevo email sending with three-strategy fallback; one table per destination with an airline column |
| `config_store.py` | Unified config layout: load/normalize/validate/save, used by `main.py` and the web app |
| `webapp.py` | Streamlit settings app |

### RyanairScraper endpoints

All direct HTTP to `ryanair.com/api/farfnd/v4` (no session cookie needed):

- `roundTripFares` — `search_round_trips`, the fast path
- `oneWayFares` — `search_flights`, two-phase detailed search
- `oneWayFares/{o}/{d}/cheapestPerDay` — `get_cheapest_per_day`, two-phase pre-filter

All three raise on HTTP errors (404 counts as empty) so `FlightFilter` can count failures. The `flyan` library and the `booking/v4/availability` fallback were removed: flyan was broken on the prod host and silently skipped, and the availability call fired on every empty farfnd result.

### EmailNotifier fallback chain

1. `brevo-python` v4 SDK
2. `sib-api-v3-sdk`
3. Direct HTTP POST to `api.brevo.com/v3/smtp/email`

### Log files

Results are **prepended** to `logs/flight_finder.log` (newest run always at the top). This is intentional — `main.py:prepend_to_file` reads the existing content and writes new content before it (atomically, via a temp file), keeping only the newest `logging.max_result_runs` runs (default 365).

Diagnostics (per-request info lines, failed requests, email errors) go to a separate size-rotated file, `logging.debug_log_file` (default `logs/flight_finder_debug.log`; `max_log_size_mb` / `backup_count` apply to it). WARNING+ also goes to stderr, which cron appends to `logs/cron_errors.log`.

## Configuration

`config.yaml` is **gitignored** — bootstrap a local copy from `config.yaml.example`. All search parameters live there:
- `search.origin` — departure airport IATA code (default: `BUD`)
- `search.morning_before` / `search.evening_after` — time window for outbound/return flights
- `search.trip_mode` — `daytrip` (same-day out-and-back, the default) or `multiday` (morning outbound on day 1, evening return `min_nights`–`max_nights` later). `FlightFilter._night_range` turns this into the nights to search (`daytrip` = `[0]`); every candidate is an `(D1, D2 = D1 + n)` pair. An unknown value is rejected at startup. `DayTrip.return_date`/`.nights` carry the span (nights `0` = day trip).
- `search.min_nights` / `search.max_nights` — night range, `multiday` only
- `search.destinations` — explicit list; empty means fetch all routes from the API
- `search.exclude_destinations` — always-excluded IATA codes
- `search.currency` / `search.max_price` — apply to every airline; there is no per-airline currency (price limits would not be comparable).
- `airlines.<key>.enabled` / `.max_price` — the same two keys for every airline (`ryanair`, `wizzair`, `easyjet`, `eurowings`, `jet2`, `norwegian`, `pegasus`, `ajet`, `airbaltic`). `max_price` is that airline's own round-trip limit; `null` falls back to `search.max_price`. Implemented as `BaseScraper.max_price`, which the fast path prefers over the global limit.
- `airlines.<key>.destinations` — `"all"` or a list of IATA codes, for every airline. `"all"` means every route in the airline's `GOOGLE_FLIGHTS_AIRLINES[...]["routes"]` list; for Ryanair it means no restriction (its routes come from the API) and a list sets `RyanairScraper.only_destinations`. Resolved by `config_store.resolve_destinations`. If `search.destinations` is set, a code must be in that list too.
- `airlines.ryanair.request_delay` / `.max_retries` — delay between Ryanair searches; retries on 429/5xx
- `google_flights.request_delay` — delay between Google Flights requests, shared by all Google Flights airlines
- `email.enabled` — set to `true` to send results via Brevo; requires `brevo_api_key`
- `email.recipient_emails` — **list** of recipient addresses (multi-recipient supported)

## Wizz Air and the other low-cost airlines go through Google Flights

`wizzair.com` and `be.wizzair.com` sit behind **AWS WAF with an image-recognition CAPTCHA** ("Choose all the beds"); plain `requests` and headless Chromium (with or without `playwright-stealth`) both fail there. Do not re-attempt wizzair.com directly with free tooling — it is a known dead end, and solving it means bypassing their access control.

Instead `scrapers/google_flights_scraper.py` reads flights from the public Google Flights search page via the `fast-flights` library (it builds the `tfs` query and parses the HTML; our own fetch adds a fixed consent cookie, because the library's cookie helper does not get past the EU "Before you continue" page). The same mechanism covers every airline in `GOOGLE_FLIGHTS_AIRLINES`: Wizz Air, easyJet, Eurowings, Jet2, Norwegian, Pegasus, AJet, airBaltic.

- **One request = one route, one day, one direction** (~2 MB of HTML), and the response lists *every* airline on that route. `GoogleFlightsClient` does the fetching (rate limit, per-run cache keyed by route and day, block detection) and is shared by all `GoogleFlightsScraper` instances — one per enabled airline, each keeping only rows whose airline name matches its registry `match` string (direct flights, no codeshares). A route flown by two airlines costs one request.
- There is no "all destinations" call and no route list, so each airline only searches its `airlines.<key>.destinations`. Volume is routes × days (27 routes × 30 days ≈ 810+ requests, ~45 min at the default 3 s `google_flights.request_delay`); the web app shows the current estimate.
- It uses the fast path (`supports_round_trip_search = True`): per date pair it fetches the outbound day for each destination, and the return day only when a morning outbound within `max_price` exists. `take_sub_request_stats()` reports the real (non-cached) request count to `FlightFilter`.
- **Stops on the first block signal**: a 429, a `/sorry/` redirect or the consent page sets a blocked flag on the client; every later uncached lookup raises `GoogleFlightsBlocked` without sending anything, which shows up as failed requests in the log/email warning.
- Google's terms disallow automated access, and the page is server-rendered HTML that can change — treat these scrapers as best-effort. An empty server response is retried once; no flight numbers are available.
- Only same-airline pairs are built; Ryanair rows on the Google pages are ignored (Ryanair has its own scraper).
- Probed 2026-10-10: apart from Wizz Air, these airlines mostly leave Budapest in the afternoon or evening (their aircraft are based elsewhere), so with the "morning out, evening back" window they rarely produce pairs.

To add another Google Flights airline: add an `Airline` enum value and a `GOOGLE_FLIGHTS_AIRLINES` entry (key, Google's display name in lower case as `match`, `routes` = every known route from BUD, optional `destinations` = default selection) and make sure each route code has a `CITY_NAMES` entry. The route lists are a snapshot from Wikipedia (July 2026) and need manual upkeep. `config_store.normalize` and the web app pick it up automatically.

## Adding a new scraper

1. Subclass `BaseScraper` in `scrapers/`.
2. Implement `get_destinations(origin)` and `search_flights(origin, dest, date, time_from, time_to)`.
3. Either set `supports_round_trip_search = True` and implement `search_round_trips` (fast path; must raise on network errors), or override `get_cheapest_per_day` for the two-phase pre-filter to work (without it, that scraper contributes no candidates).
4. Enable it in `config.yaml` under `airlines` and instantiate it in `main.py:build_scrapers`. (An airline that is on Google Flights needs none of this — see the registry note above.)

## Dependencies

- Python 3.9+
- `pyyaml`, `requests`, `pydantic` — required
- `streamlit` — only needed for `webapp.py`
- `fast-flights` (pulls in `primp`, `selectolax`, `protobuf`) — only needed when a Google Flights airline is enabled; imported lazily
- `brevo` / `sib-api-v3-sdk` — optional; `notifier.py` falls back to direct HTTP without them

`requirements.txt` lists only the required packages (plus `fast-flights` for the Google Flights airlines), pinned to the versions on the prod host; install with `pip install -r requirements.txt`.

## Deployment

`.github/workflows/deploy.yml` runs on every push to `main`: it SSHes into the DigitalOcean prod host (`134.209.226.208`, user `gdaniel1979`) and runs `git pull` in `/home/gdaniel1979/my_projects/flight_finder_ryanair`. There is **no** `pip install` or `systemctl restart` step — dependencies and the cron/systemd unit are managed manually on the host. If a change introduces a new dependency, install it on the host before merging.
