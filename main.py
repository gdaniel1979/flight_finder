"""
Flight Finder – Orchestrátor.

Futtatás:
    python3.9 main.py
    python3.9 main.py --config custom_config.yaml
    python3.9 main.py --date 2026-05-01
    python3.9 main.py --destinations BCN,BGY,STN
"""

import argparse
import csv
import json
import logging
import os
import sys
import tempfile
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import yaml

from models import SearchConfig, DayTrip
from scrapers.ryanair_scraper import RyanairScraper
from scrapers.base_scraper import BaseScraper
from filter import FlightFilter


# ── Logging: csak konzolra, fájlba prepend módban ──

def setup_logging(config: dict) -> str:
    """Logging beállítása. Visszaadja a log fájl elérési útját."""
    log_config = config.get("logging", {})
    level_name = log_config.get("level", "INFO").upper()
    # A scraper belső logokat elnyomjuk hacsak nem DEBUG
    level = getattr(logging, level_name, logging.INFO)
    log_file = log_config.get("log_file", "logs/flight_finder.log")

    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    formatter = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Konzol: csak WARNING+, a lényeg a print()-ekkel megy ki
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Scraper belső logok csendesítése konzolon
    logging.getLogger("urllib3").setLevel(logging.ERROR)
    logging.getLogger("requests").setLevel(logging.ERROR)
    logging.getLogger("scraper.ryanair-api").setLevel(logging.WARNING)

    return log_file


def prepend_to_file(filepath: str, content: str) -> None:
    """Tartalom beszúrása a fájl elejére (prepend)."""
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            old_content = f.read()
    else:
        old_content = ""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content + old_content)


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
        ryanair_currency = airlines_config["ryanair"].get("currency", currency)
        scrapers.append(RyanairScraper(currency=ryanair_currency))

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

def save_results_csv(trips: List[DayTrip], output_dir: str = "output") -> str:
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(output_dir, f"day_trips_{timestamp}.csv")

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Dátum", "Célállomás", "Célváros",
            "Oda járatszám", "Oda indulás", "Oda érkezés",
            "Vissza járatszám", "Vissza indulás", "Vissza érkezés",
            "Oda ár", "Vissza ár", "Összár", "Pénznem",
            "Légitársaság", "Forrás",
        ])
        for trip in trips:
            o = trip.outbound
            i = trip.inbound
            writer.writerow([
                trip.trip_date.isoformat(),
                o.destination,
                o.destination_city or "?",
                o.flight_number or "?",
                o.departure_time.strftime("%H:%M"),
                o.arrival_time.strftime("%H:%M") if o.arrival_time else "?",
                i.flight_number or "?",
                i.departure_time.strftime("%H:%M"),
                i.arrival_time.strftime("%H:%M") if i.arrival_time else "?",
                f"{o.price:.2f}" if o.price else "N/A",
                f"{i.price:.2f}" if i.price else "N/A",
                f"{trip.total_price:.2f}" if trip.total_price else "N/A",
                o.currency,
                o.airline.value,
                o.source,
            ])
    return filename


def save_results_json(trips: List[DayTrip], output_dir: str = "output") -> str:
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(output_dir, f"day_trips_{timestamp}.json")

    data = []
    for trip in trips:
        data.append({
            "date": trip.trip_date.isoformat(),
            "destination": trip.outbound.destination,
            "destination_city": trip.outbound.destination_city,
            "outbound": {
                "flight_number": trip.outbound.flight_number,
                "departure": trip.outbound.departure_time.isoformat(),
                "arrival": trip.outbound.arrival_time.isoformat() if trip.outbound.arrival_time else None,
                "price": trip.outbound.price,
            },
            "inbound": {
                "flight_number": trip.inbound.flight_number,
                "departure": trip.inbound.departure_time.isoformat(),
                "arrival": trip.inbound.arrival_time.isoformat() if trip.inbound.arrival_time else None,
                "price": trip.inbound.price,
            },
            "total_price": trip.total_price,
            "currency": trip.outbound.currency,
            "airline": trip.outbound.airline.value,
            "source": trip.outbound.source,
        })

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return filename


def format_results(trips: List[DayTrip], config: SearchConfig, duration_sec: float) -> str:
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
    result_text = format_results(trips, search_config, duration)

    # Konzolra
    print(result_text)

    # Log fájlba prepend (új felülre)
    prepend_to_file(log_file, result_text)

    # CSV + JSON -- Nincs szükség egyelőre, hogy létrehozza az outputot.
    # if trips:
    #     csv_file = save_results_csv(trips)
    #     json_file = save_results_json(trips)
    #     print(f"  CSV: {csv_file}")
    #     print(f"  JSON: {json_file}")

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
            success = notifier.send_day_trips(trips)
            print(f"  Email: {'OK' if success else 'HIBA'} → {', '.join(recipient_emails)}")
        else:
            print("  Email: hiányos konfiguráció")

    print(f"Kész ({duration:.0f}s)")


if __name__ == "__main__":
    main()
