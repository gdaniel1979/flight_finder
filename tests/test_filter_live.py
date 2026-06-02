"""
Éles szűrő teszt – pár célállomásra keres napi oda-vissza járatpárokat.

Futtatás: python3.9 tests/test_filter_live.py
"""

import sys
import os
import logging
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import SearchConfig
from scrapers.ryanair_scraper import RyanairScraper
from filter import FlightFilter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logging.getLogger("urllib3").setLevel(logging.WARNING)


def main():
    # Konfiguráció – itt állíthatod az időkorlátokat
    config = SearchConfig(
        origin="BUD",
        morning_before=9,   # Reggeli határ: 9:00 előtt kell indulni
        evening_after=18,    # Esti határ: 18:00 után kell visszaindulni
        search_days=30,
        currency="EUR",
        max_price=None,      # None = nincs ár limit
    )

    scraper = RyanairScraper(currency=config.currency)
    flight_filter = FlightFilter(config=config, scrapers=[scraper])

    # Teszt: csak 3 célállomás, 3 nap – hogy gyors legyen
    test_destinations = ["BCN", "BGY", "STN"]
    test_dates = [date.today() + timedelta(days=d) for d in [7, 8, 9]]

    print("=" * 70)
    print(f"Napi oda-vissza keresés")
    print(f"  Kiindulás:     {config.origin}")
    print(f"  Indulás:       {config.morning_before}:00 előtt")
    print(f"  Visszaindulás: {config.evening_after}:00 után")
    print(f"  Célállomások:  {test_destinations}")
    print(f"  Dátumok:       {test_dates[0]} – {test_dates[-1]}")
    print("=" * 70)

    trips = flight_filter.find_day_trips(
        destinations=test_destinations,
        dates=test_dates,
    )

    if not trips:
        print("\nNincs találat ezekre a feltételekre.")
        print("Ez normális – nem minden útvonalon van reggeli ÉS esti járat.")
        print("\nTipp: próbáld lazább időkorlátokkal:")
        print("  morning_before=10, evening_after=17")
    else:
        print(f"\n{'='*70}")
        print(f"TALÁLATOK: {len(trips)} járatpár")
        print(f"{'='*70}")
        for i, trip in enumerate(trips, 1):
            print(f"\n{i}. {trip.summary()}")


if __name__ == "__main__":
    main()
