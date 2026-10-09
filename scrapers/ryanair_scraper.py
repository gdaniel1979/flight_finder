"""
Ryanair scraper modul.

Közvetlen HTTP hívások a Ryanair farfnd (fare finder) API-jára:
- roundTripFares: egy kérés / nap, minden célállomásra (gyors keresés)
- oneWayFares / cheapestPerDay: útvonalankénti keresés (kétfázisú út)
"""

import logging
import time
from datetime import date, datetime
from typing import List, Optional, Dict, Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from models import Flight, DayTrip, Airline
from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

# Ryanair belső API alap URL-ek
RYANAIR_API_BASE = "https://www.ryanair.com/api"
RYANAIR_FARES_API = f"{RYANAIR_API_BASE}/farfnd/v4"
RYANAIR_LOCATE_API = f"{RYANAIR_API_BASE}/locate/v1"
RYANAIR_VIEWS_API = f"{RYANAIR_API_BASE}/views/locate"

# Rate limiting: minimum várakozás két kérés között (másodperc)
REQUEST_DELAY = 0.3            # könnyű hívások (célállomás-lista, cheapestPerDay)
REQUEST_DELAY_DETAILED = 0.8   # járatkeresések alapértéke (config: rate_limit.request_delay)
MAX_RETRIES = 3                # alapérték (config: rate_limit.max_retries)


def _create_session(max_retries: int = MAX_RETRIES) -> requests.Session:
    """HTTP session létrehozása retry logikával."""
    session = requests.Session()
    retry_strategy = Retry(
        total=max_retries,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    return session


class RyanairScraper(BaseScraper):
    """
    Ryanair járat scraper.

    Használat:
        scraper = RyanairScraper(currency="EUR")
        destinations = scraper.get_destinations("BUD")
        trips = scraper.search_round_trips("BUD", date(2026, 5, 1), date(2026, 5, 1))
    """

    def __init__(
        self,
        currency: str = "EUR",
        request_delay: float = REQUEST_DELAY_DETAILED,
        max_retries: int = MAX_RETRIES,
    ):
        super().__init__(
            airline=Airline.RYANAIR,
            source_name="ryanair-api",
            currency=currency,
        )
        self._session = _create_session(max_retries)
        self._request_delay = request_delay
        self._last_request_time = 0.0
        self._destinations_cache: Dict[str, List[str]] = {}

    def _rate_limit(self, slow: bool = False):
        """Egyszerű rate limiter. slow=True a járatkereséseknél."""
        delay = self._request_delay if slow else min(REQUEST_DELAY, self._request_delay)
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request_time = time.time()

    # ──────────────────────────────────────────────
    # 1. Célállomások lekérdezése
    # ──────────────────────────────────────────────

    def get_destinations(self, origin: str) -> List[str]:
        """BUD-ról elérhető Ryanair célállomások IATA kódjai."""
        if origin in self._destinations_cache:
            return self._destinations_cache[origin]

        destinations = self._get_destinations_direct_api(origin)
        if destinations:
            self._destinations_cache[origin] = destinations
        return destinations

    def _get_destinations_direct_api(self, origin: str) -> List[str]:
        """Célállomások lekérése a Ryanair route API-ból. Több URL-t is megpróbál."""
        urls = [
            f"{RYANAIR_VIEWS_API}/searchWidget/routes/en/airport/{origin}",
            f"{RYANAIR_VIEWS_API}/5/searchWidget/routes/en/airport/{origin}",
            f"{RYANAIR_API_BASE}/locate/v5/searchWidget/routes/en/airport/{origin}",
            f"{RYANAIR_API_BASE}/views/locate/searchWidget/routes/hu/airport/{origin}",
        ]

        for url in urls:
            self._rate_limit()
            try:
                self.logger.debug(f"Próba: {url}")
                resp = self._session.get(url, timeout=15)
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                data = resp.json()
                destinations = []
                for route in data:
                    if "arrivalAirport" in route:
                        airport = route["arrivalAirport"]
                        iata = airport.get("iataCode") or airport.get("code")
                        if iata:
                            destinations.append(iata)
                if destinations:
                    self.logger.info(
                        f"{origin}: {len(destinations)} Ryanair célállomás találva (URL: {url})"
                    )
                    return sorted(set(destinations))
            except requests.RequestException as e:
                self.logger.debug(f"URL sikertelen ({url}): {e}")
                continue

        self.logger.error(f"Célállomások lekérése sikertelen ({origin}): minden URL 404/hiba")
        return []

    # ──────────────────────────────────────────────
    # 2. Útvonalankénti járatkeresés (kétfázisú út)
    # ──────────────────────────────────────────────

    def search_flights(
        self,
        origin: str,
        destination: str,
        flight_date: date,
        departure_time_from: Optional[str] = None,
        departure_time_to: Optional[str] = None,
    ) -> List[Flight]:
        """
        Egy útvonal egy napjának legolcsóbb járata az időablakban (farfnd oneWayFares).
        HTTP hibánál kivételt dob (a 404 üres eredmény).
        """
        self._rate_limit(slow=True)
        date_str = flight_date.isoformat()
        params = {
            "departureAirportIataCode": origin,
            "arrivalAirportIataCode": destination,
            "outboundDepartureDateFrom": date_str,
            "outboundDepartureDateTo": date_str,
            "outboundDepartureTimeFrom": departure_time_from or "00:00",
            "outboundDepartureTimeTo": departure_time_to or "23:59",
            "adultPaxCount": 1,
            "market": "en-gb",
            "searchMode": "ALL",
            "currency": self.currency,
        }

        resp = self._session.get(f"{RYANAIR_FARES_API}/oneWayFares", params=params, timeout=15)
        if resp.status_code == 404:
            self.logger.debug(f"oneWayFares 404: {origin}→{destination}")
            return []
        resp.raise_for_status()
        data = resp.json()

        flights = []
        for fare_data in data.get("fares", []):
            try:
                flights.append(self._parse_farfnd_leg(fare_data["outbound"]))
            except Exception as e:
                self.logger.warning(f"oneWayFares parse hiba: {e}")

        self.logger.info(f"oneWayFares: {origin}→{destination} {flight_date}: {len(flights)} járat")
        return flights

    # ──────────────────────────────────────────────
    # 3. Oda-vissza keresés – egy kérés / nap, minden célállomásra
    # ──────────────────────────────────────────────

    supports_round_trip_search = True

    def search_round_trips(
        self,
        origin: str,
        out_date: date,
        back_date: date,
        before_hour: int = 9,
        after_hour: int = 18,
        max_price: Optional[float] = None,
        destinations: Optional[List[str]] = None,
    ) -> List[DayTrip]:
        """
        Járatpárok a farfnd roundTripFares végpontról: célállomásonként a
        legolcsóbb reggeli oda + esti vissza pár az adott napokra.
        HTTP hibánál kivételt dob.
        """
        if before_hour <= 0:
            return []

        self._rate_limit(slow=True)
        nights = (back_date - out_date).days
        params = {
            "departureAirportIataCode": origin,
            "outboundDepartureDateFrom": out_date.isoformat(),
            "outboundDepartureDateTo": out_date.isoformat(),
            "inboundDepartureDateFrom": back_date.isoformat(),
            "inboundDepartureDateTo": back_date.isoformat(),
            "durationFrom": nights,
            "durationTo": nights,
            "outboundDepartureTimeFrom": "00:00",
            "outboundDepartureTimeTo": f"{before_hour - 1:02d}:59",
            "inboundDepartureTimeFrom": f"{after_hour:02d}:00",
            "inboundDepartureTimeTo": "23:59",
            "adultPaxCount": 1,
            "market": "en-gb",
            "searchMode": "ALL",
            "currency": self.currency,
        }
        if max_price is not None:
            params["priceValueTo"] = max_price

        resp = self._session.get(
            f"{RYANAIR_FARES_API}/roundTripFares", params=params, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("nextPage") is not None:
            self.logger.warning(
                f"roundTripFares {out_date}→{back_date}: lapozott válasz, csak az első oldal feldolgozva"
            )

        trips = []
        for fare_data in data.get("fares", []):
            try:
                outbound = self._parse_farfnd_leg(fare_data["outbound"])
                inbound = self._parse_farfnd_leg(fare_data["inbound"])
            except Exception as e:
                self.logger.warning(f"roundTripFares parse hiba: {e}")
                continue

            if not outbound.is_morning_departure(before_hour):
                continue
            if not inbound.is_evening_departure(after_hour):
                continue

            trips.append(DayTrip(
                outbound=outbound,
                inbound=inbound,
                trip_date=out_date,
                return_date=back_date,
            ))

        self.logger.info(f"roundTripFares: {origin} {out_date}→{back_date}: {len(trips)} pár")
        return trips

    def _parse_farfnd_leg(self, leg: Dict[str, Any]) -> Flight:
        """Egy farfnd járat-szakasz (outbound/inbound) → Flight."""
        dep_airport = leg.get("departureAirport") or {}
        arr_airport = leg.get("arrivalAirport") or {}
        price_data = leg.get("price") or {}
        arr_str = leg.get("arrivalDate")

        price = price_data.get("value")

        fn = leg.get("flightNumber") or None
        if fn and not any(fn.startswith(p) for p in ("FR", "RK")):
            fn = f"FR{fn}"

        return Flight(
            airline=Airline.RYANAIR,
            flight_number=fn,
            origin=dep_airport["iataCode"],
            destination=arr_airport["iataCode"],
            origin_city=(dep_airport.get("city") or {}).get("name") or dep_airport.get("name"),
            destination_city=(arr_airport.get("city") or {}).get("name") or arr_airport.get("name"),
            departure_time=datetime.fromisoformat(leg["departureDate"].replace("Z", "")),
            arrival_time=datetime.fromisoformat(arr_str.replace("Z", "")) if arr_str else None,
            price=float(price) if price else None,
            currency=price_data.get("currencyCode", self.currency),
            source="ryanair-farfnd",
        )

    # ──────────────────────────────────────────────
    # 4. Fares API – egyszerűbb ár-keresés (kiegészítő)
    # ──────────────────────────────────────────────

    def get_cheapest_per_day(
        self, origin: str, destination: str, date_from: date, date_to: date
    ) -> Dict[str, Optional[float]]:
        """
        Napi legolcsóbb ár lekérése a Ryanair fares API-ból.
        Egyetlen hívással visszaadja a teljes időszakra a napi árakat.
        HTTP hibánál kivételt dob (a 404 üres eredmény).
        """
        self._rate_limit()
        url = (
            f"{RYANAIR_FARES_API}/oneWayFares/{origin}/{destination}/cheapestPerDay"
            f"?outboundDateFrom={date_from.isoformat()}"
            f"&outboundDateTo={date_to.isoformat()}"
        )
        resp = self._session.get(url, timeout=15)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        data = resp.json()
        result = {}
        for fare in data.get("outbound", {}).get("fares", []):
            fare_date = fare.get("day") or (fare.get("departureDate", "") or "")[:10]
            price_data = fare.get("price", {})
            amount = price_data.get("value") if isinstance(price_data, dict) else None
            if fare_date and amount is not None:
                result[fare_date] = float(amount)
        return result
