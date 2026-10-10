"""
Google Flights alapú scraper – Wizz Air, easyJet, Eurowings, Jet2, Norwegian stb.

Ezeknek a légitársaságoknak nincs szabadon elérhető saját API-juk (a wizzair.com
pl. AWS WAF CAPTCHA mögött van, lásd CLAUDE.md), ezért a járatokat a Google Flights
nyilvános keresőoldaláról olvassuk ki a `fast-flights` könyvtárral.

Egy kérés = egy útvonal, egy nap, egy irány – és a válasz az útvonal ÖSSZES
légitársaságát tartalmazza. Ezért egy közös `GoogleFlightsClient` tölti le és
gyorsítótárazza az oldalakat, a légitársaságonkénti `GoogleFlightsScraper` pedig
csak kiszűri belőlük a saját járatait. Két légitársaság közös útvonala így egy kérés.

Ha a Google korlátozza a kéréseket (CAPTCHA, 429, sütifal), a kliens a futás
hátralévő részére leáll, és nem próbálkozik tovább.
"""

import logging
import re
import time
from collections import namedtuple
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

# Minimum várakozás két Google kérés között (másodperc); config: google_flights.request_delay
REQUEST_DELAY = 3.0

# A Google Flights-on keresztül elérhető fapados légitársaságok.
#   match: a Google által kiírt légitársaság-név (kisbetűvel)
#   routes: a Budapestről repült összes ismert útvonal (Wikipedia, 2026. július) – ebből
#           lehet választani a webappban, és ezt jelenti a "minden útvonal"
#   destinations: az alapértelmezett kiválasztás a configban
GOOGLE_FLIGHTS_AIRLINES = {
    "wizzair": {
        "airline": Airline.WIZZAIR, "match": "wizz air",
        "routes": [
            "AUH", "ALC", "AMM", "ESB", "ATH", "GYD", "BCN", "BRI", "BSL", "BGO", "BER", "BIO", "BLL",
            "GHV", "BBU", "CRL", "CTA", "RMO", "CPH", "DTM", "DXB", "EIN", "FNC", "GDN", "HRG", "IST",
            "JED", "KLX", "KUT", "LCA", "LIS", "LGW", "LTN", "MAD", "AGP", "MLA", "RAK", "FMM", "MXP",
            "NAP", "NCE", "ORY", "TGD", "KEF", "FCO", "SSH", "SKP", "SOF", "ARN", "STR", "TLL", "TGM",
            "TLV", "TFS", "SKG", "TRN", "VLC", "VCE", "VNO", "WAW", "WRO", "EVN",
            # szezonális
            "AYT", "BOJ", "CFU", "DBV", "GOA", "SPX", "LPA", "SUF", "PMI", "RMI", "QSR", "TIA", "ZAD",
        ],
        "destinations": ["FCO", "NAP", "CTA", "ALC", "AGP", "GDN", "MLA", "BER"],
    },
    "easyjet": {
        "airline": Airline.EASYJET, "match": "easyjet",
        "routes": ["LGW", "SEN", "CDG", "LYS", "NTE", "BOD", "GVA", "BSL"],
    },
    "eurowings": {
        "airline": Airline.EUROWINGS, "match": "eurowings",
        "routes": ["DUS", "CGN", "HAM", "STR"],
    },
    "jet2": {
        "airline": Airline.JET2, "match": "jet2",
        "routes": ["MAN", "BHX", "EMA", "LBA"],
    },
    "norwegian": {
        "airline": Airline.NORWEGIAN, "match": "norwegian",
        "routes": ["CPH", "OSL", "ARN"],
    },
    "pegasus": {
        "airline": Airline.PEGASUS, "match": "pegasus",
        "routes": ["SAW"],
    },
    "ajet": {
        "airline": Airline.AJET, "match": "ajet",
        "routes": ["SAW"],
    },
    "airbaltic": {
        "airline": Airline.AIRBALTIC, "match": "airbaltic",
        "routes": ["RIX"],
    },
}
for _spec in GOOGLE_FLIGHTS_AIRLINES.values():
    _spec.setdefault("destinations", list(_spec["routes"]))

# A Google oldala nem ad városnevet a járatokhoz
CITY_NAMES = {
    "AGP": "Malaga", "ALC": "Alicante", "AMM": "Amman", "ARN": "Stockholm", "ATH": "Athens",
    "AUH": "Abu Dhabi", "AYT": "Antalya", "BBU": "Bucharest", "BCN": "Barcelona", "BER": "Berlin",
    "BGO": "Bergen", "BHX": "Birmingham", "BIO": "Bilbao", "BLL": "Billund", "BOD": "Bordeaux",
    "BOJ": "Burgas", "BRI": "Bari", "BSL": "Basel", "BUD": "Budapest", "CDG": "Paris",
    "CFU": "Corfu", "CGN": "Cologne", "CPH": "Copenhagen", "CRL": "Charleroi", "CTA": "Catania",
    "DBV": "Dubrovnik", "DTM": "Dortmund", "DUS": "Dusseldorf", "DXB": "Dubai", "EIN": "Eindhoven",
    "EMA": "East Midlands", "ESB": "Ankara", "EVN": "Yerevan", "FCO": "Rome", "FMM": "Memmingen",
    "FNC": "Funchal", "GDN": "Gdansk", "GHV": "Brasov", "GOA": "Genoa", "GVA": "Geneva",
    "GYD": "Baku", "HAM": "Hamburg", "HRG": "Hurghada", "IST": "Istanbul", "JED": "Jeddah",
    "KEF": "Reykjavik", "KLX": "Kalamata", "KUT": "Kutaisi", "LBA": "Leeds", "LCA": "Larnaca",
    "LGW": "London", "LIS": "Lisbon", "LPA": "Gran Canaria", "LTN": "London", "LYS": "Lyon",
    "MAD": "Madrid", "MAN": "Manchester", "MLA": "Malta", "MXP": "Milan", "NAP": "Naples",
    "NCE": "Nice", "NTE": "Nantes", "ORY": "Paris", "OSL": "Oslo", "PMI": "Palma de Mallorca",
    "QSR": "Salerno", "RAK": "Marrakesh", "RIX": "Riga", "RMI": "Rimini", "RMO": "Chisinau",
    "SAW": "Istanbul", "SEN": "London", "SKG": "Thessaloniki", "SKP": "Skopje", "SOF": "Sofia",
    "SPX": "Giza", "SSH": "Sharm El Sheikh", "STR": "Stuttgart", "SUF": "Lamezia Terme",
    "TFS": "Tenerife", "TGD": "Podgorica", "TGM": "Targu Mures", "TIA": "Tirana", "TLL": "Tallinn",
    "TLV": "Tel Aviv", "TRN": "Turin", "VCE": "Venice", "VLC": "Valencia", "VNO": "Vilnius",
    "WAW": "Warsaw", "WRO": "Wroclaw", "ZAD": "Zadar",
}

# Egy járatsor a Google Flights oldalról (a fast-flights Flight objektumának releváns mezői)
Row = namedtuple("Row", "name departure arrival price stops")


class GoogleFlightsBlocked(Exception):
    """A Google korlátozta a kéréseket – a futás hátralévő részében nem kérdezünk."""


class GoogleFlightsClient:
    """
    Közös letöltő a Google Flights-ra épülő scraperekhez: rate limit, futásonkénti
    gyorsítótár (útvonal-nap szerint) és leállás az első korlátozásnál.
    """

    def __init__(self, currency: str = "EUR", request_delay: float = REQUEST_DELAY):
        self.currency = currency
        self.logger = logging.getLogger("scraper.google-flights")
        self.total_requests = 0
        self.failed_requests = 0
        self._request_delay = request_delay
        self._last_request_time = 0.0
        self._blocked_reason: Optional[str] = None
        self._cache: Dict[Tuple[str, str, date], List[Row]] = {}

    def rows(self, origin: str, destination: str, flight_date: date) -> List[Row]:
        """
        Az útvonal-nap összes járatsora (minden légitársaság). Hálózati hibánál vagy
        Google-korlátozásnál kivételt dob; a sikertelen kérés nem kerül a gyorsítótárba.
        """
        key = (origin, destination, flight_date)
        if key in self._cache:
            return self._cache[key]
        if self._blocked_reason:
            raise GoogleFlightsBlocked(self._blocked_reason)

        self.total_requests += 1
        try:
            rows = self._fetch(origin, destination, flight_date)
            if not rows:
                # A szerver oldali válasz néha üresen jön – egy ismétlés
                rows = self._fetch(origin, destination, flight_date)
        except Exception:
            self.failed_requests += 1
            raise

        self._cache[key] = rows
        airlines = sorted({row.name for row in rows})
        self.logger.info(
            f"Google Flights: {origin}→{destination} {flight_date}: {len(rows)} járatsor"
            + (f" ({', '.join(airlines)})" if airlines else "")
        )
        return rows

    def _fetch(self, origin: str, destination: str, flight_date: date) -> List[Row]:
        """Egy Google Flights oldal letöltése és a közvetlen járatok sorainak kiolvasása."""
        # A könyvtár csak engedélyezett Google Flights-keresésnél kell, ezért itt importáljuk
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
            flights = ff_core.parse_response(resp).flights
        except RuntimeError:
            # "No flights found" – aznap nincs járat (vagy üres szerver oldali válasz)
            return []
        return [Row(f.name, f.departure, f.arrival, f.price, f.stops) for f in flights]

    def _block(self, reason: str) -> None:
        self._blocked_reason = reason
        self.logger.error(f"Google Flights keresés leállítva: {reason}")
        raise GoogleFlightsBlocked(reason)

    def _rate_limit(self) -> None:
        elapsed = time.time() - self._last_request_time
        if elapsed < self._request_delay:
            time.sleep(self._request_delay - elapsed)
        self._last_request_time = time.time()


class GoogleFlightsScraper(BaseScraper):
    """
    Egy légitársaság járatpárjai a Google Flights-ról, a configban megadott célállomásokra.

    Használat:
        client = GoogleFlightsClient()
        scraper = GoogleFlightsScraper("wizzair", client, destinations=["FCO", "NAP"])
        trips = scraper.search_round_trips("BUD", date(2026, 11, 4), date(2026, 11, 4))
    """

    supports_round_trip_search = True

    def __init__(
        self,
        airline_key: str,
        client: GoogleFlightsClient,
        destinations: List[str],
        max_price: Optional[float] = None,
    ):
        spec = GOOGLE_FLIGHTS_AIRLINES[airline_key]
        super().__init__(
            airline=spec["airline"],
            source_name=f"{airline_key}-google-flights",
            currency=client.currency,
        )
        self._client = client
        self._match = spec["match"]
        self._destinations = [d.strip().upper() for d in destinations]
        self.max_price = max_price
        self._sub_total = 0
        self._sub_failed = 0

    def get_destinations(self, origin: str) -> List[str]:
        """A configban megadott célállomások (útvonallista nem kérdezhető le)."""
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

        self.logger.info(f"{self.airline.value}: {origin} {out_date}→{back_date}: {len(trips)} pár")
        return trips

    def _flights_or_empty(self, origin: str, destination: str, flight_date: date) -> List[Flight]:
        """
        Egy útvonal-nap járatai. Egyedi hibánál üres lista és számolt sikertelen kérés;
        Google-korlátozásnál a kivétel továbbmegy, hogy a hívó az egész napot hibásnak lássa.
        """
        try:
            return self.search_flights(origin, destination, flight_date)
        except GoogleFlightsBlocked:
            raise
        except Exception as e:
            self.logger.error(f"Sikertelen lekérdezés – {origin}→{destination} {flight_date}: {e}")
            return []

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
        A nap összes közvetlen járata az útvonalon ettől a légitársaságtól (az időablakot
        a hívó szűri). Hálózati hibánál vagy Google-korlátozásnál kivételt dob.
        """
        requests_before = self._client.total_requests
        failed_before = self._client.failed_requests
        try:
            rows = self._client.rows(origin, destination, flight_date)
        finally:
            # Csak a ténylegesen kiküldött (nem gyorsítótárból jött) kérések számítanak
            self._sub_total += self._client.total_requests - requests_before
            self._sub_failed += self._client.failed_requests - failed_before

        flights: Dict[Tuple[datetime, Optional[float]], Flight] = {}
        for row in rows:
            if not self._is_own(row):
                continue
            try:
                dep_time = self._parse_datetime(row.departure, flight_date)
            except ValueError as e:
                self.logger.warning(f"Google Flights parse hiba ({row.departure!r}): {e}")
                continue
            try:
                arr_time = self._parse_datetime(row.arrival, flight_date)
            except ValueError:
                arr_time = None

            price = self._parse_price(row.price)
            flights[(dep_time, price)] = Flight(
                airline=self.airline,
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

        return sorted(flights.values(), key=lambda f: f.departure_time)

    def _is_own(self, row: Row) -> bool:
        """Közvetlen járat, amelyet kizárólag ez a légitársaság üzemeltet (codeshare nem)."""
        name = (row.name or "").lower()
        return row.stops == 0 and self._match in name and "," not in name

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
