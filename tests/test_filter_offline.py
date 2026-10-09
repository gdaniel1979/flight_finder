"""
Offline tesztek a gyors (roundTripFares) keresésre – hálózati hívás nélkül.

Futtatás: python -m pytest tests/test_filter_offline.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timedelta

from models import Flight, DayTrip, SearchConfig, Airline
from filter import FlightFilter
from scrapers.base_scraper import BaseScraper
from scrapers.ryanair_scraper import RyanairScraper


def _trip(dest: str, d_out: date, d_back: date, out_price: float, in_price: float) -> DayTrip:
    def leg(o, d, when, price):
        return Flight(
            airline=Airline.RYANAIR, origin=o, destination=d,
            departure_time=when, price=price, source="fake",
        )
    return DayTrip(
        outbound=leg("BUD", dest, datetime.combine(d_out, datetime.min.time()).replace(hour=6), out_price),
        inbound=leg(dest, "BUD", datetime.combine(d_back, datetime.min.time()).replace(hour=20), in_price),
        trip_date=d_out,
        return_date=d_back,
    )


class FakeRoundTripScraper(BaseScraper):
    """Minden napra BGY (olcsó) és STN (drága) párt ad; a fail_on napokon hibát dob."""

    supports_round_trip_search = True

    def __init__(self, fail_on=()):
        super().__init__(airline=Airline.RYANAIR, source_name="fake")
        self.fail_on = set(fail_on)
        self.calls = []

    def get_destinations(self, origin):
        return ["BGY", "STN"]

    def search_flights(self, *args, **kwargs):
        raise AssertionError("a gyors úton nem szabad útvonalankénti keresést hívni")

    def search_round_trips(self, origin, out_date, back_date, before_hour=9, after_hour=18, max_price=None):
        self.calls.append((out_date, back_date))
        if out_date in self.fail_on:
            raise RuntimeError("403 Forbidden")
        return [
            _trip("BGY", out_date, back_date, 20.0, 30.0),
            _trip("STN", out_date, back_date, 80.0, 90.0),
        ]


D1 = date(2026, 5, 15)
D2 = date(2026, 5, 16)


def test_fast_daytrip_one_request_per_day():
    scraper = FakeRoundTripScraper()
    f = FlightFilter(SearchConfig(max_price=100), [scraper])
    trips = f.find_trips(destinations=["BGY", "STN"], dates=[D2, D1])

    assert scraper.calls == [(D1, D1), (D2, D2)]
    # STN (170) kiesik a max_price miatt
    assert [(t.outbound.destination, t.trip_date) for t in trips] == [("BGY", D1), ("BGY", D2)]
    assert f.total_requests == 2 and f.failed_requests == 0


def test_fast_destination_filter_and_empty_list():
    f = FlightFilter(SearchConfig(), [FakeRoundTripScraper()])
    assert {t.outbound.destination for t in f.find_trips(destinations=["STN"], dates=[D1])} == {"STN"}
    # Üres / None célállomás-lista = nincs szűrés
    assert len(f.find_trips(destinations=[], dates=[D1])) == 2
    assert len(f.find_trips(destinations=None, dates=[D1])) == 2


def test_fast_multiday_date_pairs():
    scraper = FakeRoundTripScraper()
    config = SearchConfig(trip_mode="multiday", min_nights=2, max_nights=3)
    trips = FlightFilter(config, [scraper]).find_trips(destinations=["BGY"], dates=[D1])

    assert scraper.calls == [(D1, D1 + timedelta(days=2)), (D1, D1 + timedelta(days=3))]
    assert [t.nights for t in trips] == [2, 3]


def test_fast_counts_failed_requests():
    f = FlightFilter(SearchConfig(), [FakeRoundTripScraper(fail_on=[D1])])
    trips = f.find_trips(destinations=["BGY"], dates=[D1, D2])

    assert [t.trip_date for t in trips] == [D2]
    assert f.total_requests == 2 and f.failed_requests == 1


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.params = None

    def get(self, url, params=None, timeout=None):
        self.url, self.params = url, params
        return _FakeResponse(self.payload)


def _leg(o, o_city, d, d_city, dep, number, price):
    return {
        "departureAirport": {"iataCode": o, "name": o_city, "city": {"name": o_city}},
        "arrivalAirport": {"iataCode": d, "name": d_city + " Airport", "city": {"name": d_city}},
        "departureDate": dep,
        "arrivalDate": None,
        "flightNumber": number,
        "price": {"value": price, "currencyCode": "EUR"},
    }


def test_ryanair_round_trip_parsing():
    payload = {
        "nextPage": None,
        "fares": [
            {
                "outbound": _leg("BUD", "Budapest", "BGY", "Bergamo", "2026-05-15T06:20:00", "FR2108", 69.99),
                "inbound": _leg("BGY", "Bergamo", "BUD", "Budapest", "2026-05-15T18:25:00", "FR3164", 16.99),
            },
            {   # 09:10-es indulás: kívül esik a reggeli ablakon, ki kell szűrni
                "outbound": _leg("BUD", "Budapest", "STN", "London", "2026-05-15T09:10:00", "FR1", 10.0),
                "inbound": _leg("STN", "London", "BUD", "Budapest", "2026-05-15T20:00:00", "FR2", 10.0),
            },
        ],
    }
    scraper = RyanairScraper(currency="EUR")
    scraper._session = _FakeSession(payload)

    trips = scraper.search_round_trips("BUD", D1, D1, before_hour=9, after_hour=18, max_price=100)

    p = scraper._session.params
    assert scraper._session.url.endswith("/roundTripFares")
    assert p["durationFrom"] == 0 and p["durationTo"] == 0
    assert p["outboundDepartureTimeTo"] == "08:59" and p["inboundDepartureTimeFrom"] == "18:00"
    assert p["priceValueTo"] == 100

    assert len(trips) == 1
    t = trips[0]
    assert (t.outbound.destination, t.outbound.destination_city) == ("BGY", "Bergamo")
    assert (t.outbound.flight_number, t.inbound.flight_number) == ("FR2108", "FR3164")
    assert abs(t.total_price - 86.98) < 0.001
    assert t.nights == 0
