"""
Offline tesztek a Google Flights alapú scraperre (Wizz Air, easyJet, …) – hálózati hívás nélkül.

Futtatás: python -m pytest tests/test_google_flights_offline.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime

import pytest

from models import Airline, SearchConfig
from filter import FlightFilter
from scrapers.google_flights_scraper import (
    CITY_NAMES, GOOGLE_FLIGHTS_AIRLINES, GoogleFlightsBlocked, GoogleFlightsClient, GoogleFlightsScraper, Row,
)

D1 = date(2026, 11, 4)

# (honnan, hova) → a Google Flights oldal járatsorai (minden légitársaság egy oldalon)
PAGES = {
    ("BUD", "FCO"): [
        Row("Wizz Air", "6:05 AM on Wed, Nov 4", "7:55 AM on Wed, Nov 4", "€34", 0),
        Row("Wizz Air", "6:05 AM on Wed, Nov 4", "7:55 AM on Wed, Nov 4", "€34", 0),   # duplikált sor
        Row("Wizz Air", "7:00 PM on Wed, Nov 4", "8:50 PM on Wed, Nov 4", "€29", 0),
        Row("easyJet", "7:10 AM on Wed, Nov 4", "9:00 AM on Wed, Nov 4", "€20", 0),
        Row("Lufthansa, easyJet", "7:30 AM on Wed, Nov 4", "9:20 AM on Wed, Nov 4", "€15", 0),  # codeshare
        Row("Wizz Air", "5:00 AM on Wed, Nov 4", "11:00 AM on Wed, Nov 4", "€10", 1),  # átszállásos
    ],
    ("FCO", "BUD"): [
        Row("Wizz Air", "8:35 AM on Wed, Nov 4", "10:25 AM on Wed, Nov 4", "€70", 0),
        Row("Wizz Air", "9:30 PM on Wed, Nov 4", "11:20 PM on Wed, Nov 4", "€41", 0),
        Row("easyJet", "8:15 PM on Wed, Nov 4", "10:05 PM on Wed, Nov 4", "€25", 0),
    ],
    ("BUD", "NAP"): [   # csak délutáni járat: a visszautat le sem kell kérni
        Row("Wizz Air", "2:25 PM on Wed, Nov 4", "4:10 PM on Wed, Nov 4", "€60", 0),
    ],
    ("BUD", "BER"): [   # a reggeli odaút már magában drágább a limitnél
        Row("Wizz Air", "6:30 AM on Wed, Nov 4", "8:00 AM on Wed, Nov 4", "€1,250", 0),
    ],
}


def make_client(pages=PAGES, fail=()):
    client = GoogleFlightsClient(request_delay=0)
    client.fetched = []

    def fake_fetch(origin, destination, flight_date):
        client.fetched.append((origin, destination))
        if (origin, destination) in fail:
            raise RuntimeError("Google Flights HTTP 500")
        return pages.get((origin, destination), [])

    client._fetch = fake_fetch
    return client


def make_scraper(destinations, airline="wizzair", client=None, **kwargs):
    return GoogleFlightsScraper(airline, client or make_client(**kwargs), destinations=destinations)


def test_registry_is_consistent():
    for key, spec in GOOGLE_FLIGHTS_AIRLINES.items():
        assert isinstance(spec["airline"], Airline)
        assert spec["match"] == spec["match"].lower()
        assert spec["routes"] and all(len(code) == 3 and code.isupper() for code in spec["routes"])
        assert len(set(spec["routes"])) == len(spec["routes"])
        assert spec["destinations"] and set(spec["destinations"]) <= set(spec["routes"])
        assert all(code in CITY_NAMES for code in spec["routes"])


def test_search_flights_keeps_only_own_direct_flights():
    flights = make_scraper(["FCO"]).search_flights("BUD", "FCO", D1)

    assert [f.departure_time for f in flights] == [datetime(2026, 11, 4, 6, 5), datetime(2026, 11, 4, 19, 0)]
    first = flights[0]
    assert first.airline == Airline.WIZZAIR and first.price == 34.0
    assert first.arrival_time == datetime(2026, 11, 4, 7, 55)
    assert first.destination_city == "Rome"

    # Ugyanarról az oldalról az easyJet csak a saját járatát kapja, a codeshare-t nem
    easyjet = make_scraper(["FCO"], airline="easyjet").search_flights("BUD", "FCO", D1)
    assert [(f.airline, f.price) for f in easyjet] == [(Airline.EASYJET, 20.0)]


def test_parse_helpers():
    assert GoogleFlightsScraper._parse_price("€1,250") == 1250.0
    assert GoogleFlightsScraper._parse_price("Price unavailable") is None
    # Decemberi keresés januári érkezése a következő évre esik
    assert GoogleFlightsScraper._parse_datetime("12:10 AM on Fri, Jan 1", date(2026, 12, 31)) == datetime(2027, 1, 1, 0, 10)
    with pytest.raises(ValueError):
        GoogleFlightsScraper._parse_datetime("soon", D1)


def test_round_trips_pair_morning_out_with_evening_back():
    scraper = make_scraper(["FCO", "NAP", "BER"])
    trips = scraper.search_round_trips("BUD", D1, D1, before_hour=9, after_hour=18, max_price=100)

    assert len(trips) == 1
    trip = trips[0]
    assert (trip.outbound.departure_time.hour, trip.inbound.departure_time.hour) == (6, 21)
    assert trip.total_price == 75.0
    # NAP-ra nincs reggeli járat, BER túl drága: egyikre sem megy vissza irányú kérés
    fetched = scraper._client.fetched
    assert ("NAP", "BUD") not in fetched and ("BER", "BUD") not in fetched
    assert scraper.take_sub_request_stats() == (4, 0)
    assert scraper.take_sub_request_stats() == (0, 0)


def test_airlines_share_one_request_per_route():
    client = make_client()
    wizz = make_scraper(["FCO"], client=client)
    easyjet = make_scraper(["FCO"], airline="easyjet", client=client)
    f = FlightFilter(SearchConfig(), [wizz, easyjet])
    trips = f.find_trips(destinations=["FCO"], dates=[D1])

    assert sorted((t.outbound.airline.value, t.total_price) for t in trips) == [("Wizz Air", 75.0), ("easyJet", 45.0)]
    # A közös útvonal oda és vissza iránya is csak egyszer lett lekérve
    assert client.fetched == [("BUD", "FCO"), ("FCO", "BUD")]
    assert f.total_requests == 2 and f.failed_requests == 0


def test_destination_filter():
    scraper = make_scraper(["FCO", "NAP"])
    scraper.search_round_trips("BUD", D1, D1, destinations=["FCO"])
    assert scraper._client.fetched == [("BUD", "FCO"), ("FCO", "BUD")]


def test_failed_request_is_counted_and_others_continue():
    scraper = make_scraper(["NAP", "FCO"], fail=[("BUD", "NAP")])
    f = FlightFilter(SearchConfig(), [scraper])
    trips = f.find_trips(destinations=["NAP", "FCO"], dates=[D1])

    assert [t.outbound.destination for t in trips] == ["FCO"]
    assert f.total_requests == 3 and f.failed_requests == 1


def test_block_stops_all_further_requests():
    client = make_client()

    def blocked_fetch(origin, destination, flight_date):
        client.fetched.append((origin, destination))
        client._block("a Google korlátozta a kéréseket (HTTP 429)")

    client._fetch = blocked_fetch
    wizz = make_scraper(["FCO"], client=client)
    easyjet = make_scraper(["LGW"], airline="easyjet", client=client)
    f = FlightFilter(SearchConfig(), [wizz, easyjet])
    assert f.find_trips(destinations=["FCO", "LGW"], dates=[D1, date(2026, 11, 5)]) == []

    # Egyetlen kérés ment ki; utána mindkét légitársaság minden napja hibaként számolódik
    assert len(client.fetched) == 1
    assert f.total_requests == 4 and f.failed_requests == 4
    with pytest.raises(GoogleFlightsBlocked):
        wizz.search_flights("BUD", "NAP", D1)


def test_scraper_max_price_overrides_global_limit():
    # A FCO pár 75 EUR: a globális 50-es limit kiszűrné, a légitársaság saját 80-as limitje átengedi
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
