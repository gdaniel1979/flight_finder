"""
Ryanair scraper modul.

Két stratégiát alkalmaz:
1. Elsődleges: 'flyan' Python könyvtár (tiszta API wrapper, idő-szűrés támogatás)
2. Fallback: Közvetlen HTTP hívások a Ryanair belső API-jára
"""

import logging
import time
from datetime import date, datetime, timedelta
from typing import List, Optional, Dict, Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from models import Flight, Airline
from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

# Ryanair belső API alap URL-ek
RYANAIR_API_BASE = "https://www.ryanair.com/api"
RYANAIR_FARES_API = f"{RYANAIR_API_BASE}/farfnd/v4"
RYANAIR_LOCATE_API = f"{RYANAIR_API_BASE}/locate/v1"
RYANAIR_VIEWS_API = f"{RYANAIR_API_BASE}/views/locate"

# Rate limiting: minimum várakozás két kérés között (másodperc)
REQUEST_DELAY = 0.3
REQUEST_DELAY_DETAILED = 0.8


def _create_session() -> requests.Session:
    """HTTP session létrehozása retry logikával."""
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
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
        flights = scraper.search_flights("BUD", "BCN", date(2026, 5, 1))
    """

    def __init__(self, currency: str = "EUR"):
        super().__init__(
            airline=Airline.RYANAIR,
            source_name="ryanair-api",
            currency=currency,
        )
        self._session = _create_session()
        self._flyan_available = self._check_flyan()
        self._last_request_time = 0.0
        self._destinations_cache: Dict[str, List[str]] = {}

        if self._flyan_available:
            self.logger.info("flyan könyvtár elérhető – elsődleges stratégia: flyan")
        else:
            self.logger.info("flyan nem elérhető – közvetlen API mód")

    def _check_flyan(self) -> bool:
        """Ellenőrzi, hogy a flyan könyvtár elérhető-e."""
        try:
            from flyan import RyanAir, FlightSearchParams
            return True
        except Exception:
            return False

    def _rate_limit(self, slow: bool = False):
        """Egyszerű rate limiter. slow=True a részletes kereséseknél."""
        delay = REQUEST_DELAY_DETAILED if slow else REQUEST_DELAY
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
    # 2. Járatkeresés – stratégia választó
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
        Járatok keresése. Először flyan-nel próbálja,
        ha az nem elérhető vagy hibát dob, fallback a direkt API-ra.
        """
        if self._flyan_available:
            try:
                return self._search_via_flyan(
                    origin, destination, flight_date,
                    departure_time_from, departure_time_to
                )
            except Exception as e:
                self.logger.warning(f"flyan hiba, fallback direkt API-ra: {e}")

        return self._search_via_direct_api(
            origin, destination, flight_date,
            departure_time_from, departure_time_to
        )

    # ──────────────────────────────────────────────
    # 2a. Keresés flyan könyvtárral
    # ──────────────────────────────────────────────

    def _search_via_flyan(
        self,
        origin: str,
        destination: str,
        flight_date: date,
        departure_time_from: Optional[str] = None,
        departure_time_to: Optional[str] = None,
    ) -> List[Flight]:
        """Járatkeresés a flyan könyvtárral."""
        from flyan import RyanAir, FlightSearchParams

        client = RyanAir(currency=self.currency)

        params = FlightSearchParams(
            from_airport=origin,
            to_airport=destination,
            from_date=datetime.combine(flight_date, datetime.min.time()),
            to_date=datetime.combine(flight_date, datetime.max.time()),
            departure_time_from=departure_time_from or "00:00",
            departure_time_to=departure_time_to or "23:59",
        )

        raw_flights = client.get_oneways(params)
        flights = []

        for rf in raw_flights:
            try:
                dep_time = rf.departure_date if isinstance(rf.departure_date, datetime) else datetime.combine(flight_date, datetime.min.time())

                flight = Flight(
                    airline=Airline.RYANAIR,
                    flight_number=getattr(rf, "flight_number", None),
                    origin=origin,
                    destination=destination,
                    origin_city=getattr(rf, "departure_airport", {}).get("name") if hasattr(rf, "departure_airport") and isinstance(rf.departure_airport, dict) else getattr(getattr(rf, "departure_airport", None), "name", None),
                    destination_city=getattr(rf, "arrival_airport", {}).get("name") if hasattr(rf, "arrival_airport") and isinstance(rf.arrival_airport, dict) else getattr(getattr(rf, "arrival_airport", None), "name", None),
                    departure_time=dep_time,
                    price=getattr(rf, "price", None),
                    currency=getattr(rf, "currency", self.currency),
                    source="ryanair-flyan",
                )
                flights.append(flight)
            except Exception as e:
                self.logger.warning(f"flyan eredmény feldolgozási hiba: {e}")

        self.logger.info(
            f"flyan: {origin}→{destination} {flight_date}: {len(flights)} járat"
        )
        return flights

    # ──────────────────────────────────────────────
    # 2b. Keresés közvetlen API hívással (fallback)
    # ──────────────────────────────────────────────

    def _search_via_direct_api(
        self,
        origin: str,
        destination: str,
        flight_date: date,
        departure_time_from: Optional[str] = None,
        departure_time_to: Optional[str] = None,
    ) -> List[Flight]:
        """
        Járatkeresés közvetlen API hívásokkal.
        Két módszert próbál:
        1. farfnd (fare finder) API – nem igényel session cookie-t
        2. availability API – session cookie kellhet hozzá
        """
        flights = self._search_via_farfnd(
            origin, destination, flight_date,
            departure_time_from, departure_time_to
        )
        if flights:
            return flights

        return self._search_via_availability(
            origin, destination, flight_date,
            departure_time_from, departure_time_to
        )

    def _search_via_farfnd(
        self,
        origin: str,
        destination: str,
        flight_date: date,
        departure_time_from: Optional[str] = None,
        departure_time_to: Optional[str] = None,
    ) -> List[Flight]:
        """Járatkeresés a fare finder API-val (nem igényel session cookie-t)."""
        self._rate_limit(slow=True)
        date_str = flight_date.strftime("%Y-%m-%d")
        time_from = departure_time_from or "00:00"
        time_to = departure_time_to or "23:59"

        url = (
            f"{RYANAIR_FARES_API}/oneWayFares"
            f"?departureAirportIataCode={origin}"
            f"&arrivalAirportIataCode={destination}"
            f"&outboundDepartureDateFrom={date_str}"
            f"&outboundDepartureDateTo={date_str}"
            f"&outboundDepartureTimeFrom={time_from}"
            f"&outboundDepartureTimeTo={time_to}"
            f"&adultPaxCount=1&market=en-gb&searchMode=ALL"
            f"&currency={self.currency}"
        )

        try:
            resp = self._session.get(url, timeout=15)
            if resp.status_code == 404:
                self.logger.debug(f"farfnd 404: {origin}→{destination}")
                return []
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            self.logger.debug(f"farfnd hiba ({origin}→{destination}): {e}")
            return []

        flights = []
        fares_list = data.get("fares", [])

        for fare_data in fares_list:
            try:
                outbound = fare_data.get("outbound", {})
                dep_str = outbound.get("departureDate", "")
                arr_str = outbound.get("arrivalDate", "")
                price_data = outbound.get("price", {})

                if not dep_str:
                    continue

                dep_time = datetime.fromisoformat(dep_str.replace("Z", ""))
                arr_time = datetime.fromisoformat(arr_str.replace("Z", "")) if arr_str else None

                price = price_data.get("value") or price_data.get("valueMainUnit")
                currency = price_data.get("currencyCode", self.currency)

                # Az ár lehet float (14.99) vagy string ("14")
                if price is not None:
                    price = float(price)

                flight_number = outbound.get("flightNumber", "")
                dep_airport = outbound.get("departureAirport", {})
                arr_airport = outbound.get("arrivalAirport", {})

                # A flightNumber már tartalmazhatja a carrier prefixet (pl. "FR2273")
                fn = flight_number if flight_number else None
                if fn and not any(fn.startswith(p) for p in ("FR", "RK")):
                    fn = f"FR{fn}"

                flight = Flight(
                    airline=Airline.RYANAIR,
                    flight_number=fn,
                    origin=origin,
                    destination=destination,
                    origin_city=dep_airport.get("city", {}).get("name") if isinstance(dep_airport.get("city"), dict) else dep_airport.get("name"),
                    destination_city=arr_airport.get("city", {}).get("name") if isinstance(arr_airport.get("city"), dict) else arr_airport.get("name"),
                    departure_time=dep_time,
                    arrival_time=arr_time,
                    price=price if price else None,
                    currency=currency,
                    source="ryanair-farfnd",
                )
                flights.append(flight)
            except Exception as e:
                self.logger.warning(f"farfnd parse hiba: {e}")

        self.logger.info(f"farfnd: {origin}→{destination} {flight_date}: {len(flights)} járat")
        return flights

    def _search_via_availability(
        self,
        origin: str,
        destination: str,
        flight_date: date,
        departure_time_from: Optional[str] = None,
        departure_time_to: Optional[str] = None,
    ) -> List[Flight]:
        """
        Járatkeresés a Ryanair availability API-jával.
        Megjegyzés: ez session cookie-t igényelhet, nem mindig működik.
        """
        self._rate_limit(slow=True)

        date_str = flight_date.strftime("%Y-%m-%d")
        url = (
            f"https://www.ryanair.com/api/booking/v4/en-gb/availability"
            f"?ADT=1&CHD=0&DateOut={date_str}"
            f"&Destination={destination}&Disc=0"
            f"&INF=0&Origin={origin}&TEEN=0"
            f"&promoCode=&IncludeConnectingFlights=false"
            f"&FlexDaysBeforeOut=0&FlexDaysOut=0&FlexDaysBeforeIn=0&FlexDaysIn=0"
            f"&RoundTrip=false&ToUs=AGREED"
        )

        try:
            resp = self._session.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, dict) and data.get("message", "").lower().startswith("availability declined"):
                self.logger.warning("Availability declined – session cookie szükséges")
                return []
        except requests.RequestException as e:
            self.logger.debug(f"Availability API hiba ({origin}→{destination}): {e}")
            return []

        flights = []
        trips = data.get("trips", [])

        for trip in trips:
            for trip_date_data in trip.get("dates", []):
                for flight_data in trip_date_data.get("flights", []):
                    flight = self._parse_availability_flight(
                        flight_data, origin, destination, flight_date,
                        departure_time_from, departure_time_to
                    )
                    if flight:
                        flights.append(flight)

        self.logger.info(
            f"availability: {origin}→{destination} {flight_date}: {len(flights)} járat"
        )
        return flights

    def _parse_availability_flight(
        self,
        flight_data: Dict[str, Any],
        origin: str,
        destination: str,
        flight_date: date,
        departure_time_from: Optional[str] = None,
        departure_time_to: Optional[str] = None,
    ) -> Optional[Flight]:
        """Egyetlen járat parse-olása az availability API válaszból."""
        try:
            time_parts = flight_data.get("time", [])
            if not time_parts or len(time_parts) < 1:
                return None

            dep_str = time_parts[0]
            dep_time = datetime.fromisoformat(dep_str.replace("Z", "+00:00"))
            if dep_time.tzinfo:
                dep_time = dep_time.replace(tzinfo=None)

            arr_time = None
            if len(time_parts) > 1 and time_parts[1]:
                arr_str = time_parts[1]
                arr_time = datetime.fromisoformat(arr_str.replace("Z", "+00:00"))
                if arr_time.tzinfo:
                    arr_time = arr_time.replace(tzinfo=None)

            # Idő szűrés alkalmazása
            if departure_time_from:
                from_h, from_m = map(int, departure_time_from.split(":"))
                if dep_time.hour < from_h or (dep_time.hour == from_h and dep_time.minute < from_m):
                    return None
            if departure_time_to:
                to_h, to_m = map(int, departure_time_to.split(":"))
                if dep_time.hour > to_h or (dep_time.hour == to_h and dep_time.minute > to_m):
                    return None

            # Legolcsóbb ár keresése
            price = None
            regular_fare = flight_data.get("regularFare")
            if regular_fare and "fares" in regular_fare:
                fares = regular_fare["fares"]
                prices = [
                    f["amount"] for f in fares
                    if f.get("amount") is not None and f["amount"] > 0
                ]
                if prices:
                    price = min(prices)

            flight_number = flight_data.get("flightNumber", "")
            carrier = flight_data.get("operatedBy") or "FR"

            return Flight(
                airline=Airline.RYANAIR,
                flight_number=f"{carrier}{flight_number}" if flight_number else None,
                origin=origin,
                destination=destination,
                departure_time=dep_time,
                arrival_time=arr_time,
                price=price,
                currency=self.currency,
                source="ryanair-api",
            )
        except Exception as e:
            self.logger.warning(f"Járat parse hiba: {e}")
            return None

    # ──────────────────────────────────────────────
    # 3. Fares API – egyszerűbb ár-keresés (kiegészítő)
    # ──────────────────────────────────────────────

    def get_cheapest_per_day(
        self, origin: str, destination: str, date_from: date, date_to: date
    ) -> Dict[str, Optional[float]]:
        """
        Napi legolcsóbb ár lekérése a Ryanair fares API-ból.
        Egyetlen hívással visszaadja a teljes időszakra a napi árakat.
        """
        self._rate_limit()
        url = (
            f"{RYANAIR_FARES_API}/oneWayFares/{origin}/{destination}/cheapestPerDay"
            f"?outboundDateFrom={date_from.isoformat()}"
            f"&outboundDateTo={date_to.isoformat()}"
        )
        try:
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
        except requests.RequestException as e:
            self.logger.debug(f"cheapestPerDay hiba ({origin}→{destination}): {e}")
            return {}