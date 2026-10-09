"""
Wizz Air scraper modul – Google Flights-on keresztül.

A wizzair.com AWS WAF CAPTCHA mögött van (lásd CLAUDE.md), ezért a járatokat
a Google Flights nyilvános keresőoldaláról olvassuk ki a `fast-flights`
könyvtárral. Egy kérés = egy útvonal, egy nap, egy irány, ezért csak a
configban megadott rövid célállomás-listára keres.

Ha a Google korlátozza a kéréseket (CAPTCHA, 429, sütifal), a scraper a futás
hátralévő részére leáll, és nem próbálkozik tovább.
"""

import re
import time
from datetime import date, datetime
from itertools import product as cartesian_product
from typing import Dict, List, Optional, Tuple

from models import Flight, DayTrip, Airline
from scrapers.base_scraper import BaseScraper

GOOGLE_FLIGHTS_URL = "https://www.google.com/travel/flights"

# Rögzített sütielfogadás – enélkül az EU-ból a "Before you continue" oldal jön
CONSENT_COOKIES = {
    "SOCS": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg",
    "CONSENT": "YES+cb.20210720-07-p0.en+FX+410",
}

# Minimum várakozás két Google kérés között (másodperc); config: airlines.wizzair.request_delay
REQUEST_DELAY = 3.0

# A Google oldala csak városnevet nem ad a járatokhoz – a Wizz budapesti célállomásai
CITY_NAMES = {
    "AGP": "Malaga", "ALC": "Alicante", "ARN": "Stockholm", "ATH": "Athens", "BBU": "Bucharest",
    "BCN": "Barcelona", "BER": "Berlin", "BGO": "Bergen", "BIO": "Bilbao", "BLL": "Billund",
    "BRI": "Bari", "BSL": "Basel", "BUD": "Budapest", "CPH": "Copenhagen", "CRL": "Charleroi",
    "CTA": "Catania", "DTM": "Dortmund", "EIN": "Eindhoven", "FCO": "Rome", "FMM": "Memmingen",
    "FNC": "Funchal", "GDN": "Gdansk", "IST": "Istanbul", "KEF": "Reykjavik", "LCA": "Larnaca",
    "LGW": "London", "LIS": "Lisbon", "LTN": "London", "MAD": "Madrid", "MLA": "Malta",
    "MXP": "Milan", "NAP": "Naples", "NCE": "Nice", "ORY": "Paris", "SKG": "Thessaloniki",
    "SOF": "Sofia", "STR": "Stuttgart", "TFS": "Tenerife", "TLL": "Tallinn", "TRN": "Turin",
    "VCE": "Venice", "VLC": "Valencia", "VNO": "Vilnius", "WAW": "Warsaw", "WRO": "Wroclaw",
}


class GoogleFlightsBlocked(Exception):
    """A Google korlátozta a kéréseket – a futás hátralévő részében nem kérdezünk."""


class WizzairScraper(BaseScraper):
    """
    Wizz Air járatpárok a Google Flights-ról.

    Használat:
        scraper = WizzairScraper(destinations=["FCO", "NAP"])
        trips = scraper.search_round_trips("BUD", date(2026, 11, 4), date(2026, 11, 4))
    """

    supports_round_trip_search = True

    def __init__(
        self,
        destinations: List[str],
        currency: str = "EUR",
        request_delay: float = REQUEST_DELAY,
    ):
        super().__init__(
            airline=Airline.WIZZAIR,
            source_name="wizzair-google-flights",
            currency=currency,
        )
        self._destinations = [d.strip().upper() for d in destinations]
        self._request_delay = request_delay
        self._last_request_time = 0.0
        self._blocked_reason: Optional[str] = None
        self._cache: Dict[Tuple[str, str, date], List[Flight]] = {}
        self._sub_total = 0
        self._sub_failed = 0

    def get_destinations(self, origin: str) -> List[str]:
        """A configban megadott célállomások (a Wizz útvonallistája nem kérdezhető le)."""
        return list(self._destinations)

    def take_sub_request_stats(self) -> Tuple[int, int]:
        stats = (self._sub_total, self._sub_failed)
        self._sub_total = self._sub_failed = 0
        return stats

    # ──────────────────────────────────────────────
    # Oda-vissza keresés: célállomásonként 1-2 kérés
    # ──────────────────────────────────────────────

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
        if self._blocked_reason:
            raise GoogleFlightsBlocked(self._blocked_reason)

        wanted = self._destinations
        if destinations is not None:
            wanted = [d for d in wanted if d in set(destinations)]

        trips: List[DayTrip] = []
        for dest in wanted:
            outbound = [
                f for f in self._flights_or_empty(origin, dest, out_date)
                if f.is_morning_departure(before_hour)
            ]
            if not outbound:
                continue
            # Ha már az odaút is drágább a limitnél, a visszautat fölösleges lekérni
            if max_price is not None and all(f.price is not None and f.price > max_price for f in outbound):
                continue

            inbound = [
                f for f in self._flights_or_empty(dest, origin, back_date)
                if f.is_evening_departure(after_hour)
            ]
            for out_flight, in_flight in cartesian_product(outbound, inbound):
                trips.append(DayTrip(
                    outbound=out_flight,
                    inbound=in_flight,
                    trip_date=out_date,
                    return_date=back_date,
                ))

        self.logger.info(f"Google Flights: {origin} {out_date}→{back_date}: {len(trips)} pár")
        return trips

    def _flights_or_empty(self, origin: str, destination: str, flight_date: date) -> List[Flight]:
        """Egy útvonal-nap járatai; hibánál üres lista és számolt sikertelen kérés."""
        key = (origin, destination, flight_date)
        if key in self._cache:
            return self._cache[key]

        self._sub_total += 1
        try:
            flights = self.search_flights(origin, destination, flight_date)
        except GoogleFlightsBlocked:
            self._sub_failed += 1
            raise
        except Exception as e:
            self._sub_failed += 1
            self.logger.error(f"Sikertelen lekérdezés – {origin}→{destination} {flight_date}: {e}")
            return []

        self._cache[key] = flights
        return flights

    # ──────────────────────────────────────────────
    # Egy útvonal egy napja
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
        A nap összes közvetlen Wizz Air járata az útvonalon (az időablakot a hívó szűri).
        Hálózati hibánál vagy Google-korlátozásnál kivételt dob.
        """
        if self._blocked_reason:
            raise GoogleFlightsBlocked(self._blocked_reason)

        raw_flights = self._fetch(origin, destination, flight_date)
        if not raw_flights:
            # A szerver oldali válasz néha üresen jön – egy ismétlés
            raw_flights = self._fetch(origin, destination, flight_date)

        flights: Dict[Tuple[datetime, Optional[float]], Flight] = {}
        for raw in raw_flights:
            if "wizz" not in (raw.name or "").lower() or raw.stops != 0:
                continue
            try:
                dep_time = self._parse_datetime(raw.departure, flight_date)
            except ValueError as e:
                self.logger.warning(f"Google Flights parse hiba ({raw.departure!r}): {e}")
                continue
            try:
                arr_time = self._parse_datetime(raw.arrival, flight_date)
            except ValueError:
                arr_time = None

            price = self._parse_price(raw.price)
            flights[(dep_time, price)] = Flight(
                airline=Airline.WIZZAIR,
                origin=origin,
                destination=destination,
                origin_city=CITY_NAMES.get(origin),
                destination_city=CITY_NAMES.get(destination),
                departure_time=dep_time,
                arrival_time=arr_time,
                price=price,
                currency=self.currency,
                source=self.source_name,
            )

        result = sorted(flights.values(), key=lambda f: f.departure_time)
        self.logger.info(f"Google Flights: {origin}→{destination} {flight_date}: {len(result)} Wizz járat")
        return result

    def _fetch(self, origin: str, destination: str, flight_date: date) -> list:
        """Egy Google Flights oldal letöltése és a járatsorok kiolvasása."""
        # A könyvtár csak engedélyezett Wizz-keresésnél kell, ezért itt importáljuk
        from fast_flights import FlightData, Passengers
        from fast_flights import core as ff_core
        from fast_flights.filter import TFSData

        tfs = TFSData.from_interface(
            flight_data=[FlightData(
                date=flight_date.isoformat(), from_airport=origin, to_airport=destination,
            )],
            trip="one-way",
            passengers=Passengers(adults=1),
            seat="economy",
            max_stops=0,
        )
        params = {
            "tfs": tfs.as_b64().decode("utf-8"),
            "hl": "en",
            "tfu": "EgQIABABIgA",
            "curr": self.currency,
        }

        self._rate_limit()
        client = ff_core.Client(impersonate="chrome_126", verify=True)
        resp = client.get(GOOGLE_FLIGHTS_URL, params=params, cookies=CONSENT_COOKIES)

        url = str(resp.url)
        if resp.status_code == 429 or "/sorry/" in url:
            self._block(f"a Google korlátozta a kéréseket (HTTP {resp.status_code})")
        if "consent.google" in url:
            self._block("a Google sütielfogadó oldala jött vissza (a rögzített süti már nem érvényes)")
        if resp.status_code != 200:
            raise RuntimeError(f"Google Flights HTTP {resp.status_code}")

        try:
            return ff_core.parse_response(resp).flights
        except RuntimeError:
            # "No flights found" – aznap nincs járat (vagy üres szerver oldali válasz)
            return []

    def _block(self, reason: str) -> None:
        self._blocked_reason = reason
        self.logger.error(f"Wizz Air keresés leállítva: {reason}")
        raise GoogleFlightsBlocked(reason)

    def _rate_limit(self) -> None:
        elapsed = time.time() - self._last_request_time
        if elapsed < self._request_delay:
            time.sleep(self._request_delay - elapsed)
        self._last_request_time = time.time()

    @staticmethod
    def _parse_datetime(text: str, flight_date: date) -> datetime:
        """'6:00 AM on Wed, Oct 21' → datetime; az évet a keresett nap adja."""
        match = re.match(r"\s*(\d{1,2}:\d{2}\s*[AP]M)\s+on\s+\w+,\s+(\w+)\s+(\d{1,2})", text or "")
        if not match:
            raise ValueError("ismeretlen időformátum")
        clock = datetime.strptime(re.sub(r"\s+", " ", match.group(1)), "%I:%M %p").time()
        month = datetime.strptime(match.group(2)[:3], "%b").month
        day = int(match.group(3))
        # Decemberi indulás januári érkezése a következő évre esik
        year = flight_date.year + (1 if month < flight_date.month else 0)
        return datetime.combine(date(year, month, day), clock)

    @staticmethod
    def _parse_price(text: str) -> Optional[float]:
        """'€85' / '€1,234' → float; 'Price unavailable' → None."""
        digits = re.sub(r"[^\d.]", "", (text or "").replace(",", ""))
        return float(digits) if digits else None
