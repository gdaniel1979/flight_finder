"""
Offline tesztek a Wizz Air (Google Flights) scraperre – hálózati hívás nélkül.

Futtatás: python -m pytest tests/test_wizzair_offline.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections import namedtuple
from datetime import date, datetime

import pytest

from models import Airline, SearchConfig
from filter import FlightFilter
from scrapers.wizzair_scraper import WizzairScraper, GoogleFlightsBlocked

Row = namedtuple("Row", "name departure arrival price stops")

D1 = date(2026, 11, 4)

# (honnan, hova) → a Google Flights oldal járatsorai
PAGES = {
    ("BUD", "FCO"): [
        Row("Wizz Air", "6:05 AM on Wed, Nov 4", "7:55 AM on Wed, Nov 4", "€34", 0),
        Row("Wizz Air", "6:05 AM on Wed, Nov 4", "7:55 AM on Wed, Nov 4", "€34", 0),   # duplikált sor
        Row("Wizz Air", "7:00 PM on Wed, Nov 4", "8:50 PM on Wed, Nov 4", "€29", 0),
        Row("Ryanair", "7:10 AM on Wed, Nov 4", "9:00 AM on Wed, Nov 4", "€20", 0),    # nem Wizz
        Row("Wizz Air", "5:00 AM on Wed, Nov 4", "11:00 AM on Wed, Nov 4", "€10", 1),  # átszállásos
    ],
    ("FCO", "BUD"): [
        Row("Wizz Air", "8:35 AM on Wed, Nov 4", "10:25 AM on Wed, Nov 4", "€70", 0),
        Row("Wizz Air", "9:30 PM on Wed, Nov 4", "11:20 PM on Wed, Nov 4", "€41", 0),
    ],
    ("BUD", "NAP"): [   # csak délutáni járat: a visszautat le sem kell kérni
        Row("Wizz Air", "2:25 PM on Wed, Nov 4", "4:10 PM on Wed, Nov 4", "€60", 0),
    ],
    ("BUD", "BER"): [   # a reggeli odaút már magában drágább a limitnél
        Row("Wizz Air", "6:30 AM on Wed, Nov 4", "8:00 AM on Wed, Nov 4", "€1,250", 0),
    ],
}


def make_scraper(destinations, pages=PAGES, fail=()):
    scraper = WizzairScraper(destinations=destinations, request_delay=0)
    scraper.fetched = []

    def fake_fetch(origin, destination, flight_date):
        scraper.fetched.append((origin, destination))
        if (origin, destination) in fail:
            raise RuntimeError("Google Flights HTTP 500")
        return pages.get((origin, destination), [])

    scraper._fetch = fake_fetch
    return scraper


def test_search_flights_parses_only_direct_wizz():
    flights = make_scraper(["FCO"]).search_flights("BUD", "FCO", D1)

    assert [f.departure_time for f in flights] == [datetime(2026, 11, 4, 6, 5), datetime(2026, 11, 4, 19, 0)]
    first = flights[0]
    assert first.airline == Airline.WIZZAIR and first.price == 34.0
    assert first.arrival_time == datetime(2026, 11, 4, 7, 55)
    assert first.destination_city == "Rome"


def test_parse_helpers():
    assert WizzairScraper._parse_price("€1,250") == 1250.0
    assert WizzairScraper._parse_price("Price unavailable") is None
    # Decemberi keresés januári érkezése a következő évre esik
    assert WizzairScraper._parse_datetime("12:10 AM on Fri, Jan 1", date(2026, 12, 31)) == datetime(2027, 1, 1, 0, 10)
    with pytest.raises(ValueError):
        WizzairScraper._parse_datetime("soon", D1)


def test_round_trips_pair_morning_out_with_evening_back():
    scraper = make_scraper(["FCO", "NAP", "BER"])
    trips = scraper.search_round_trips("BUD", D1, D1, before_hour=9, after_hour=18, max_price=100)

    assert len(trips) == 1
    trip = trips[0]
    assert (trip.outbound.departure_time.hour, trip.inbound.departure_time.hour) == (6, 21)
    assert trip.total_price == 75.0
    # NAP-ra nincs reggeli járat, BER túl drága: egyikre sem megy vissza irányú kérés
    assert ("NAP", "BUD") not in scraper.fetched and ("BER", "BUD") not in scraper.fetched
    assert scraper.take_sub_request_stats() == (4, 0)
    assert scraper.take_sub_request_stats() == (0, 0)


def test_destination_filter_and_cache():
    scraper = make_scraper(["FCO", "NAP"])
    scraper.search_round_trips("BUD", D1, D1, destinations=["FCO"])
    assert scraper.fetched == [("BUD", "FCO"), ("FCO", "BUD")]

    # Ugyanarra az útvonal-napra nem megy új kérés
    scraper.search_round_trips("BUD", D1, D1, destinations=["FCO"])
    assert len(scraper.fetched) == 2


def test_failed_request_is_counted_and_others_continue():
    scraper = make_scraper(["NAP", "FCO"], fail=[("BUD", "NAP")])
    f = FlightFilter(SearchConfig(), [scraper])
    trips = f.find_trips(destinations=["NAP", "FCO"], dates=[D1])

    assert [t.outbound.destination for t in trips] == ["FCO"]
    assert f.total_requests == 3 and f.failed_requests == 1


def test_block_stops_all_further_requests():
    scraper = make_scraper(["FCO"])

    def blocked_fetch(origin, destination, flight_date):
        scraper.fetched.append((origin, destination))
        scraper._block("a Google korlátozta a kéréseket (HTTP 429)")

    scraper._fetch = blocked_fetch
    f = FlightFilter(SearchConfig(), [scraper])
    assert f.find_trips(destinations=["FCO"], dates=[D1, date(2026, 11, 5), date(2026, 11, 6)]) == []

    # Egyetlen kérés ment ki; a többi nap kérés nélkül, hibaként számolódik
    assert len(scraper.fetched) == 1
    assert f.total_requests == 3 and f.failed_requests == 3
    with pytest.raises(GoogleFlightsBlocked):
        scraper.search_flights("BUD", "FCO", D1)


def test_scraper_max_price_overrides_global_limit():
    # A FCO pár 75 EUR: a globális 50-es limit kiszűrné, a Wizz saját 80-as limitje átengedi
    f = FlightFilter(SearchConfig(max_price=50), [make_scraper(["FCO"])])
    assert f.find_trips(destinations=["FCO"], dates=[D1]) == []

    scraper = make_scraper(["FCO"])
    scraper.max_price = 80
    f = FlightFilter(SearchConfig(max_price=50), [scraper])
    assert [t.total_price for t in f.find_trips(destinations=["FCO"], dates=[D1])] == [75.0]

    scraper = make_scraper(["FCO"])
    scraper.max_price = 60
    f = FlightFilter(SearchConfig(max_price=None), [scraper])
    assert f.find_trips(destinations=["FCO"], dates=[D1]) == []
