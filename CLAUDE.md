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

`webapp.py` is a Streamlit form over every key in `config.yaml` (tabs: Keresés, Légitársaságok, Email, Rendszer). `config_store.py` holds the logic it shares with tests: load, `validate()` (reuses `SearchConfig` plus IATA/email/airline checks), `describe_changes()` and an atomic `save_config()` that first copies the old file to `config.yaml.bak`.

- **There is no login** (the user's explicit choice on 2026-10-09: single user). Anyone who can reach the port can edit the settings, so two guards stay in place: the Brevo API key is never rendered (the field is always empty; leaving it empty keeps the stored key), and `validate()` only accepts log file paths that are relative and inside the project.
- **Saving rewrites `config.yaml` with PyYAML**, so hand-written comments in it are lost (the key descriptions stay in `config.yaml.example`). Keys the form does not know are preserved.
- Changes apply to the next run; nothing is restarted. The app never triggers a search.
- `deploy/flight-finder-web.service` is the systemd unit (port 8503); installing it needs sudo and is done by hand, like the cron job.

## Running tests

```bash
python -m pytest tests/test_models.py tests/test_filter_offline.py tests/test_wizzair_offline.py tests/test_webapp_offline.py -v   # Unit tests (no network calls)
python -m pytest tests/ -v                 # All tests (live tests hit real APIs)
python tests/test_models.py                # Run model tests directly
```

`tests/test_models.py` — offline unit tests for models and scraper instantiation.  
`tests/test_filter_offline.py` — offline tests for the fast search path (fake scraper, fake HTTP session).  
`tests/test_wizzair_offline.py` — offline tests for the Wizz Air scraper (fake Google Flights rows).  
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
| `scrapers/wizzair_scraper.py` | `WizzairScraper` — Wizz Air via Google Flights (`fast-flights`) |
| `notifier.py` | `EmailNotifier` — Brevo email sending with three-strategy fallback |
| `webapp.py` / `config_store.py` | Streamlit settings app and its config load/validate/save logic |

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
- `rate_limit.request_delay` / `rate_limit.max_retries` — passed to `RyanairScraper` (delay between flight searches; retries on 429/5xx)
- `airlines.ryanair.enabled` — the easyJet stub exists but is not implemented
- `airlines.wizzair.enabled` / `.destinations` / `.request_delay` — Wizz Air via Google Flights, only for the listed IATA codes (see WizzAir note below).
- `airlines.wizzair.max_price` — Wizz-only round-trip price limit; `null` falls back to `search.max_price`. Implemented as `BaseScraper.max_price`, which the fast path prefers over the global limit. If `search.destinations` is set, a Wizz destination must be in that list too.
- `email.enabled` — set to `true` to send results via Brevo; requires `brevo_api_key`
- `email.recipient_emails` — **list** of recipient addresses (multi-recipient supported)

## WizzAir goes through Google Flights

`wizzair.com` and `be.wizzair.com` sit behind **AWS WAF with an image-recognition CAPTCHA** ("Choose all the beds"); plain `requests` and headless Chromium (with or without `playwright-stealth`) both fail there. Do not re-attempt wizzair.com directly with free tooling — it is a known dead end, and solving it means bypassing their access control.

Instead `scrapers/wizzair_scraper.py` (`WizzairScraper`) reads Wizz Air flights from the public Google Flights search page via the `fast-flights` library (it builds the `tfs` query and parses the HTML; our own fetch adds a fixed consent cookie, because the library's cookie helper does not get past the EU "Before you continue" page).

- **One request = one route, one day, one direction** (~2 MB of HTML). There is no "all destinations" call and no route list, so it only searches `airlines.wizzair.destinations` — keep that list short (8 destinations × 30 days is 240–480 requests, ~15–25 min at the default 3 s `request_delay`).
- It uses the fast path (`supports_round_trip_search = True`): per date pair it fetches the outbound day for each destination, and the return day only when a morning outbound within `max_price` exists. Results are cached per (route, day) for the run, and `take_sub_request_stats()` reports the real request count to `FlightFilter`.
- **Stops on the first block signal**: a 429, a `/sorry/` redirect or the consent page sets a blocked flag; every later call raises `GoogleFlightsBlocked` without sending anything, which shows up as failed requests in the log/email warning.
- Google's terms disallow automated access, and the page is server-rendered HTML that can change — treat this scraper as best-effort. An empty server response is retried once; no flight numbers are available (the email shows the airline name instead).
- Only Wizz–Wizz pairs are built; Ryanair flights that appear on the same Google page are ignored (Ryanair has its own scraper).

## Adding a new scraper

1. Subclass `BaseScraper` in `scrapers/`.
2. Implement `get_destinations(origin)` and `search_flights(origin, dest, date, time_from, time_to)`.
3. Either set `supports_round_trip_search = True` and implement `search_round_trips` (fast path; must raise on network errors), or override `get_cheapest_per_day` for the two-phase pre-filter to work (without it, that scraper contributes no candidates).
4. Enable it in `config.yaml` under `airlines` and instantiate it in `main.py:build_scrapers`.

## Dependencies

- Python 3.9+
- `pyyaml`, `requests`, `pydantic` — required
- `streamlit` — only needed for `webapp.py`
- `fast-flights` (pulls in `primp`, `selectolax`, `protobuf`) — only needed when `airlines.wizzair.enabled` is true; imported lazily
- `brevo` / `sib-api-v3-sdk` — optional; `notifier.py` falls back to direct HTTP without them

`requirements.txt` lists only the required packages (plus `fast-flights` for Wizz Air), pinned to the versions on the prod host; install with `pip install -r requirements.txt`.

## Deployment

`.github/workflows/deploy.yml` runs on every push to `main`: it SSHes into the DigitalOcean prod host (`134.209.226.208`, user `gdaniel1979`) and runs `git pull` in `/home/gdaniel1979/my_projects/flight_finder_ryanair`. There is **no** `pip install` or `systemctl restart` step — dependencies and the cron/systemd unit are managed manually on the host. If a change introduces a new dependency, install it on the host before merging.
