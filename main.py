"""
Flight Finder – Orchestrátor.

Futtatás:
    python3.9 main.py
    python3.9 main.py --config custom_config.yaml
    python3.9 main.py --date 2026-05-01
    python3.9 main.py --destinations BCN,BGY,STN
"""

import argparse
import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import ValidationError

from models import SearchConfig, DayTrip
from scrapers.ryanair_scraper import RyanairScraper
from scrapers.wizzair_scraper import WizzairScraper
from scrapers.base_scraper import BaseScraper
from filter import FlightFilter


# ── Logging: eredmények prepend módban a log_file-ba, diagnosztika külön fájlba ──

def setup_logging(config: dict) -> str:
    """Logging beállítása. Visszaadja az eredmény-log fájl elérési útját."""
    log_config = config.get("logging", {})
    level_name = log_config.get("level", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = log_config.get("log_file", "logs/flight_finder.log")
    debug_log_file = log_config.get("debug_log_file", "logs/flight_finder_debug.log")

    for path in (log_file, debug_log_file):
        log_dir = os.path.dirname(path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    formatter = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Konzol: csak WARNING+, stderr-re (cron alatt a cron_errors.log-ba kerül);
    # a lényeg a print()-ekkel megy ki
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Diagnosztikai log: a beállított szinttől minden, méret szerint forgatva
    file_handler = RotatingFileHandler(
        debug_log_file,
        maxBytes=int(log_config.get("max_log_size_mb", 10) * 1024 * 1024),
        backupCount=log_config.get("backup_count", 5),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # A HTTP könyvtárakból csak a figyelmeztetések (pl. újrapróbálkozás) kellenek
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("primp").setLevel(logging.WARNING)

    return log_file


# Egy futás blokkjának eleje az eredmény-logban (lásd format_results)
RUN_HEADER_RE = re.compile(r"^─{75}\nFlight Finder \|", re.MULTILINE)


def prepend_to_file(filepath: str, content: str, max_runs: Optional[int] = None) -> None:
    """
    Tartalom beszúrása a fájl elejére (prepend). Ha max_runs meg van adva, csak a
    legutóbbi ennyi futás marad meg. Atomikus: ideiglenes fájlba ír, majd cserél.
    """
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            old_content = f.read()
    else:
        old_content = ""

    new_content = content + old_content
    if max_runs:
        run_starts = [m.start() for m in RUN_HEADER_RE.finditer(new_content)]
        if len(run_starts) > max_runs:
            new_content = new_content[:run_starts[max_runs]]

    tmp_path = f"{filepath}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    os.replace(tmp_path, filepath)


# ── Config ──

def load_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        print(f"HIBA: Konfigurációs fájl nem található: {config_path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_search_config(config: dict) -> SearchConfig:
    search = config.get("search", {})
    try:
        return _search_config_from(search)
    except ValidationError as e:
        print("HIBA: Érvénytelen keresési beállítás a konfigurációban:")
        for err in e.errors():
            field = ".".join(["search"] + [str(part) for part in err["loc"]])
            print(f"  {field}: {err['msg']}")
        sys.exit(1)


def _search_config_from(search: dict) -> SearchConfig:
    return SearchConfig(
        origin=search.get("origin", "BUD"),
        morning_before=search.get("morning_before", 9),
        evening_after=search.get("evening_after", 18),
        search_days=search.get("search_days", 30),
        currency=search.get("currency", "EUR"),
        max_price=search.get("max_price"),
        trip_mode=search.get("trip_mode", "daytrip"),
        min_nights=search.get("min_nights", 2),
        max_nights=search.get("max_nights", 4),
    )


def build_scrapers(config: dict, currency: str) -> List[BaseScraper]:
    scrapers = []
    airlines_config = config.get("airlines", {})

    if airlines_config.get("ryanair", {}).get("enabled", True):
        ryanair_currency = airlines_config.get("ryanair", {}).get("currency", currency)
        rate_limit = config.get("rate_limit", {})
        scrapers.append(RyanairScraper(
            currency=ryanair_currency,
            request_delay=rate_limit.get("request_delay", 0.8),
            max_retries=rate_limit.get("max_retries", 3),
        ))

    wizzair_config = airlines_config.get("wizzair", {})
    if wizzair_config.get("enabled", False):
        wizzair_destinations = wizzair_config.get("destinations") or []
        if wizzair_destinations:
            scrapers.append(WizzairScraper(
                destinations=wizzair_destinations,
                currency=wizzair_config.get("currency", currency),
                request_delay=wizzair_config.get("request_delay", 3),
                max_price=wizzair_config.get("max_price"),
            ))
        else:
            print("FIGYELEM: airlines.wizzair engedélyezve, de nincs megadva destinations – kihagyva")

    return scrapers


def resolve_destinations(config: dict, scrapers: List[BaseScraper], args) -> Optional[List[str]]:
    if args.destinations:
        return [d.strip().upper() for d in args.destinations.split(",")]

    search = config.get("search", {})
    configured = search.get("destinations", [])
    excluded = set(search.get("exclude_destinations", []))

    if configured:
        return [d for d in configured if d not in excluded]

    if excluded:
        all_dests = set()
        for scraper in scrapers:
            all_dests.update(scraper.get_destinations(search.get("origin", "BUD")))
        return sorted(all_dests - excluded)

    return None


def resolve_dates(args, search_days: int) -> Optional[List[date]]:
    if args.date:
        try:
            return [date.fromisoformat(args.date)]
        except ValueError:
            print(f"Érvénytelen dátum: {args.date}")
            sys.exit(1)
    return None


# ── Output ──

def format_results(
    trips: List[DayTrip],
    config: SearchConfig,
    duration_sec: float,
    warning: Optional[str] = None,
) -> str:
    """Formázott eredmény string – logba és konzolra is megy."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    lines.append(f"{'─'*75}")
    lines.append(f"Flight Finder | {now} | {duration_sec:.0f}s")
    if config.trip_mode == "multiday":
        lines.append(
            f"  {config.origin} | {config.min_nights}-{config.max_nights} éj | "
            f"indulás <{config.morning_before}:00 | vissza >{config.evening_after}:00 | {config.currency}"
        )
    else:
        lines.append(
            f"  {config.origin} | indulás <{config.morning_before}:00 | "
            f"vissza >{config.evening_after}:00 | {config.currency}"
        )
    lines.append(f"{'─'*75}")
    if warning:
        lines.append(f"  FIGYELEM: {warning}")

    if not trips:
        lines.append("  Nincs találat.")
    else:
        lines.append(f"  {len(trips)} járatpár:")
        lines.append("")
        sorted_trips = sorted(
            trips,
            key=lambda t: (
                (t.outbound.destination_city or "?").lower(),
                t.outbound.destination,
                t.trip_date,
            ),
        )
        for i, trip in enumerate(sorted_trips, 1):
            o = trip.outbound
            ink = trip.inbound
            price_str = f"{trip.total_price:.2f} {o.currency}" if trip.total_price else "N/A"
            dest_name = o.destination_city or o.destination
            if trip.nights > 0:
                date_part = f"{trip.trip_date}→{trip.return_date} ({trip.nights} éj)"
            else:
                date_part = f"{trip.trip_date}"
            lines.append(
                f"  {i:3d}. {date_part} | {o.destination} ({dest_name}) | "
                f"oda {o.departure_time.strftime('%H:%M')} | "
                f"vissza {ink.departure_time.strftime('%H:%M')} | "
                f"{price_str} | {o.airline.value}"
            )
        lines.append("")

    lines.append("")
    return "\n".join(lines)


# ── CLI ──

def parse_args():
    parser = argparse.ArgumentParser(description="Flight Finder")
    parser.add_argument("--config", "-c", default="config.yaml")
    parser.add_argument("--date", "-d", default=None, help="YYYY-MM-DD")
    parser.add_argument("--destinations", default=None, help="BCN,BGY,STN")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


# ── Main ──

def main():
    args = parse_args()
    raw_config = load_config(args.config)
    log_file = setup_logging(raw_config)
    search_config = build_search_config(raw_config)

    start_time = datetime.now()
    print(f"Flight Finder indítás: {start_time.strftime('%H:%M:%S')}")

    # Scraperek
    scrapers = build_scrapers(raw_config, search_config.currency)
    if not scrapers:
        print("HIBA: Nincs engedélyezett scraper!")
        sys.exit(1)

    # Célállomások és dátumok
    destinations = resolve_destinations(raw_config, scrapers, args)
    dates = resolve_dates(args, search_config.search_days)

    if args.dry_run:
        dest_info = f"{len(destinations)} db" if destinations else "összes"
        date_info = f"{dates}" if dates else f"következő {search_config.search_days} nap"
        print(f"DRY RUN – Célállomások: {dest_info}, Dátumok: {date_info}")
        return

    # Célállomások lekérése ha kell
    if destinations is None:
        print("Célállomások lekérése...", end=" ", flush=True)
        all_dests = set()
        for scraper in scrapers:
            all_dests.update(scraper.get_destinations(search_config.origin))
        destinations = sorted(all_dests)
        print(f"{len(destinations)} db")

    # Keresés
    date_count = len(dates) if dates else search_config.search_days
    print(
        f"Keresés: {len(destinations)} célállomás × {date_count} nap | "
        f"oda <{search_config.morning_before}:00 | vissza >{search_config.evening_after}:00"
    )

    flight_filter = FlightFilter(config=search_config, scrapers=scrapers)
    trips = flight_filter.find_trips(destinations=destinations, dates=dates)

    # Időmérés
    duration = (datetime.now() - start_time).total_seconds()

    # Eredmény formázás
    # Sikertelen lekérdezések: ne tűnjön hibátlannak egy hiányos futás
    warning = None
    if flight_filter.failed_requests:
        warning = (
            f"{flight_filter.failed_requests}/{flight_filter.total_requests} lekérdezés "
            f"sikertelen – az eredmény hiányos lehet."
        )

    result_text = format_results(trips, search_config, duration, warning)

    # Konzolra
    print(result_text)

    # Log fájlba prepend (új felülre)
    max_runs = raw_config.get("logging", {}).get("max_result_runs", 365)
    prepend_to_file(log_file, result_text, max_runs=max_runs)

    # Email – minden lefutáskor kimegy: találat esetén a járatpárokkal,
    # üres eredménynél rövid "nincs találat" visszaigazolással.
    email_config = raw_config.get("email", {})
    if email_config.get("enabled", False):
        from notifier import EmailNotifier

        api_key = email_config.get("brevo_api_key", "")
        sender_email = email_config.get("sender_email", "")
        recipient_emails = email_config.get("recipient_emails", [])
        if not recipient_emails:
            single = email_config.get("recipient_email", "")
            if single:
                recipient_emails = [single]

        if all([api_key, sender_email, recipient_emails]):
            notifier = EmailNotifier(
                api_key=api_key,
                sender_email=sender_email,
                sender_name=email_config.get("sender_name", "Flight Finder"),
                recipient_emails=recipient_emails,
            )
            success = notifier.send_day_trips(trips, warning=warning)
            print(f"  Email: {'OK' if success else 'HIBA'} → {', '.join(recipient_emails)}")
        else:
            print("  Email: hiányos konfiguráció")

    print(f"Kész ({duration:.0f}s)")


if __name__ == "__main__":
    main()
