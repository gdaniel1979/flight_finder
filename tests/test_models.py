"""
Teszt script az adatmodellek és a scraper struktúra validálására.

Futtatás: python -m pytest tests/test_models.py -v
vagy:     python tests/test_models.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, date, time
from models import Flight, DayTrip, SearchConfig, Airline


def test_flight_creation():
    """Flight modell alapvető létrehozása és validálása."""
    flight = Flight(
        airline=Airline.RYANAIR,
        flight_number="FR1234",
        origin="BUD",
        destination="BCN",
        origin_city="Budapest",
        destination_city="Barcelona",
        departure_time=datetime(2026, 5, 15, 7, 30),
        arrival_time=datetime(2026, 5, 15, 9, 45),
        price=29.99,
        currency="EUR",
        source="ryanair-api",
    )
    assert flight.origin == "BUD"
    assert flight.destination == "BCN"
    assert flight.departure_date == date(2026, 5, 15)
    assert flight.departure_time_only == time(7, 30)
    assert flight.is_morning_departure(9) is True
    assert flight.is_evening_departure(18) is False
    print("✓ Flight modell OK")


def test_evening_flight():
    """Esti járat szűrés validálása."""
    flight = Flight(
        airline=Airline.RYANAIR,
        origin="BCN",
        destination="BUD",
        departure_time=datetime(2026, 5, 15, 19, 15),
        price=34.99,
        currency="EUR",
        source="ryanair-api",
    )
    assert flight.is_morning_departure(9) is False
    assert flight.is_evening_departure(18) is True
    print("✓ Esti járat szűrés OK")


def test_day_trip():
    """DayTrip párosítás és ár-összegzés."""
    outbound = Flight(
        airline=Airline.RYANAIR,
        origin="BUD",
        destination="BCN",
        departure_time=datetime(2026, 5, 15, 6, 30),
        price=29.99,
        currency="EUR",
        source="ryanair-api",
    )
    inbound = Flight(
        airline=Airline.RYANAIR,
        origin="BCN",
        destination="BUD",
        departure_time=datetime(2026, 5, 15, 20, 30),
        price=39.99,
        currency="EUR",
        source="ryanair-api",
    )
    trip = DayTrip(outbound=outbound, inbound=inbound, trip_date=date(2026, 5, 15))
    assert trip.total_price == 69.98
    summary = trip.summary()
    assert "BUD" in summary
    assert "BCN" in summary
    assert "06:30" in summary
    assert "20:30" in summary
    print(f"✓ DayTrip OK: {summary}")


def test_search_config():
    """SearchConfig alapértékek validálása."""
    config = SearchConfig()
    assert config.origin == "BUD"
    assert config.morning_before == 9
    assert config.evening_after == 18
    assert config.search_days == 30
    print("✓ SearchConfig OK")


def test_scraper_interface():
    """RyanairScraper létrehozhatósága (import és inicializálás)."""
    from scrapers.ryanair_scraper import RyanairScraper

    scraper = RyanairScraper(currency="EUR")
    assert scraper.airline == Airline.RYANAIR
    assert scraper.source_name == "ryanair-api"
    print(f"✓ RyanairScraper OK (flyan elérhető: {scraper._flyan_available})")


if __name__ == "__main__":
    print("=" * 60)
    print("Flight Finder – Modell és struktúra tesztek")
    print("=" * 60)
    test_flight_creation()
    test_evening_flight()
    test_day_trip()
    test_search_config()
    test_scraper_interface()
    print("=" * 60)
    print("Minden teszt sikeres!")
