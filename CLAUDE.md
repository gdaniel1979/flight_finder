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

## Running tests

```bash
python -m pytest tests/test_models.py -v   # Unit tests (no network calls)
python -m pytest tests/ -v                 # All tests (live tests hit real APIs)
python tests/test_models.py                # Run model tests directly
```

`tests/test_models.py` — offline unit tests for models and scraper instantiation.  
`tests/test_ryanair_live.py` and `tests/test_filter_live.py` — make real network requests to Ryanair APIs.

## Architecture

The search runs in two phases orchestrated by `filter.py`:

**Phase 1 – Pre-filter** (`FlightFilter._prefilter_candidates`): calls `get_cheapest_per_day()` once per route direction to get a set of dates that have any flights at all. Only date/destination pairs that have flights in *both* directions survive.

**Phase 2 – Detailed search** (`FlightFilter._search_destination_date`): for each surviving (destination, date) pair, fetches morning outbound flights and evening return flights, then builds all valid `DayTrip` pairs by Cartesian product, filtering by `max_price`.

### Modules

| File | Responsibility |
|------|---------------|
| `main.py` | CLI entry point, config loading, result formatting, log prepend, email dispatch |
| `models.py` | Pydantic models: `Flight`, `DayTrip`, `SearchConfig`, `Airline` enum |
| `filter.py` | `FlightFilter` — two-phase search and trip pairing |
| `scrapers/base_scraper.py` | `BaseScraper` abstract class; provides `search_outbound_flights` / `search_return_flights` with time filtering |
| `scrapers/ryanair_scraper.py` | `RyanairScraper` — three-strategy fallback hierarchy |
| `notifier.py` | `EmailNotifier` — Brevo email sending with three-strategy fallback |

### RyanairScraper fallback chain

1. **flyan library** (`_search_via_flyan`) — preferred; requires `pip install flyan`
2. **farfnd API** (`_search_via_farfnd`) — direct HTTP to `ryanair.com/api/farfnd/v4`; no session cookie needed
3. **availability API** (`_search_via_availability`) — may require session cookie; least reliable

`get_cheapest_per_day` always uses the farfnd `cheapestPerDay` endpoint directly (no flyan involved).

### EmailNotifier fallback chain

1. `brevo-python` v4 SDK
2. `sib-api-v3-sdk`
3. Direct HTTP POST to `api.brevo.com/v3/smtp/email`

### Log file behavior

Results are **prepended** to `logs/flight_finder.log` (newest run always at the top). This is intentional — `main.py:prepend_to_file` reads the existing content and writes new content before it.

## Configuration

`config.yaml` is **gitignored** — bootstrap a local copy from `config.yaml.example`. All search parameters live there:
- `search.origin` — departure airport IATA code (default: `BUD`)
- `search.morning_before` / `search.evening_after` — time window for outbound/return flights
- `search.trip_mode` — `daytrip` (same-day out-and-back, the default) or `multiday` (morning outbound on day 1, evening return `min_nights`–`max_nights` later). `FlightFilter.find_trips` dispatches on this; `multiday` builds `(dest, D1, D2)` candidates where `D2 = D1 + n` nights and the return leg extends the prefilter window by `max_nights`. `DayTrip.return_date`/`.nights` carry the span (nights `0` = day trip).
- `search.min_nights` / `search.max_nights` — night range, `multiday` only
- `search.destinations` — explicit list; empty means fetch all routes from the API
- `search.exclude_destinations` — always-excluded IATA codes
- `airlines.ryanair.enabled` — WizzAir and easyJet stubs exist but are not implemented (see WizzAir note below)
- `email.enabled` — set to `true` to send results via Brevo; requires `brevo_api_key`
- `email.recipient_emails` — **list** of recipient addresses (multi-recipient supported)

## WizzAir is intentionally not implemented

`wizzair.com` and `be.wizzair.com` sit behind **AWS WAF with an image-recognition CAPTCHA** ("Choose all the beds"). Verified empirically:
- Plain `requests` → 405 on homepage, IIS 404 on `be.wizzair.com/<version>/Api/...`.
- Headless Chromium (with and without `playwright-stealth`) gets stuck on the WAF challenge — clicking "Begin" surfaces an image CAPTCHA that headless code cannot solve.

The only automated paths that work are paid: a scraping-proxy service (ScraperAPI, ZenRows, Bright Data) or a CAPTCHA-solving API (2captcha, Anti-Captcha) wrapping a Playwright flow. If/when one is acquired, a `WizzairScraper(BaseScraper)` slots in with no other changes — the pre-filter phase already tolerates scrapers that return empty results from `get_cheapest_per_day`.

Do not re-attempt this with free tooling — it is a known dead end.

## Adding a new scraper

1. Subclass `BaseScraper` in `scrapers/`.
2. Implement `get_destinations(origin)` and `search_flights(origin, dest, date, time_from, time_to)`.
3. Optionally override `get_cheapest_per_day` for the pre-filter phase to work (without it, that scraper contributes no candidates).
4. Enable it in `config.yaml` under `airlines` and instantiate it in `main.py:build_scrapers`.

## Dependencies

- Python 3.9+
- `pyyaml`, `requests`, `pydantic` — required
- `flyan` — optional; enables primary Ryanair search strategy
- `brevo` / `sib-api-v3-sdk` — optional; `notifier.py` falls back to direct HTTP without them

A `requirements.txt` is checked in (frozen from the prod host); install with `pip install -r requirements.txt`.

## Deployment

`.github/workflows/deploy.yml` runs on every push to `main`: it SSHes into the DigitalOcean prod host (`134.209.226.208`, user `gdaniel1979`) and runs `git pull` in `/home/gdaniel1979/my_projects/flight_finder_ryanair`. There is **no** `pip install` or `systemctl restart` step — dependencies and the cron/systemd unit are managed manually on the host. If a change introduces a new dependency, install it on the host before merging.
