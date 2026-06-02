"""
Éles Ryanair API teszt – diagnosztikai verzió.

Futtatás: python3.9 tests/test_ryanair_live.py
"""

import sys
import os
import logging
import json
from datetime import date, timedelta

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers.ryanair_scraper import RyanairScraper

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logging.getLogger("urllib3").setLevel(logging.WARNING)


def test_raw_urls():
    """Nyers URL tesztelés – megnézzük melyik endpoint él."""
    print("\n" + "=" * 60)
    print("0. Nyers URL tesztelés")
    print("=" * 60)

    urls = [
        ("routes v1", "https://www.ryanair.com/api/views/locate/searchWidget/routes/en/airport/BUD"),
        ("routes v5", "https://www.ryanair.com/api/views/locate/5/searchWidget/routes/en/airport/BUD"),
        ("locate v5", "https://www.ryanair.com/api/locate/v5/searchWidget/routes/en/airport/BUD"),
        ("farfnd cheapest", "https://www.ryanair.com/api/farfnd/v4/oneWayFares/BUD/BCN/cheapestPerDay?outboundDateFrom=2026-04-20&outboundDateTo=2026-05-20"),
        ("farfnd oneWayFares", "https://www.ryanair.com/api/farfnd/v4/oneWayFares?departureAirportIataCode=BUD&arrivalAirportIataCode=BCN&outboundDepartureDateFrom=2026-04-20&outboundDepartureDateTo=2026-04-20&adultPaxCount=1&market=en-gb&searchMode=ALL"),
        ("aggregate", "https://www.ryanair.com/api/aggregate/3/common?embedded=airports&market=en-gb"),
    ]

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json",
    })

    working_routes_url = None

    for name, url in urls:
        try:
            resp = session.get(url, timeout=15)
            status = resp.status_code
            content_type = resp.headers.get("content-type", "?")
            body_preview = resp.text[:200] if resp.text else "(empty)"
            print(f"  [{status}] {name}")
            print(f"         URL: {url}")
            print(f"         Content-Type: {content_type}")
            print(f"         Body: {body_preview}")
            if status == 200 and "routes" in name:
                working_routes_url = url
                try:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        print(f"         -> {len(data)} utvonal, elso: {json.dumps(data[0], indent=2)[:300]}")
                except Exception:
                    pass
            print()
        except Exception as e:
            print(f"  [ERR] {name}: {e}\n")

    return working_routes_url


def test_scraper():
    """Scraper modul teszt a javított URL-ekkel."""
    print("\n" + "=" * 60)
    print("1. Scraper - celallomások")
    print("=" * 60)

    scraper = RyanairScraper(currency="EUR")
    destinations = scraper.get_destinations("BUD")

    if not destinations:
        print("Celallomások: SIKERTELEN")
        return

    print(f"Talalt: {len(destinations)} celallomás")
    print(f"Lista: {destinations}")

    print("\n" + "=" * 60)
    print("2. Scraper - jaratkereses")
    print("=" * 60)

    test_date = date.today() + timedelta(days=7)

    for dest in destinations[:5]:
        print(f"\nKereses: BUD -> {dest}, {test_date}")
        flights = scraper.search_flights("BUD", dest, test_date)
        if flights:
            for f in flights:
                price_str = f"{f.price:.2f} {f.currency}" if f.price else "N/A"
                print(f"  {f.flight_number or '?':>8} | {f.departure_time.strftime('%H:%M')} | {price_str} | {f.source}")
            break
    else:
        print("Egyik tesztelt utvonalon sem talaltunk jaratot.")


if __name__ == "__main__":
    working_url = test_raw_urls()
    if working_url:
        print(f"\n-> Mukodo routes URL: {working_url}")
    test_scraper()
    print("\n" + "=" * 60)
    print("Teszt vege.")
