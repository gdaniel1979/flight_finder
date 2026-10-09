"""
Szűrő és párosító modul.

Két keresési út van:
- GYORS: egy round-trip kérés / (oda nap, vissza nap), minden célállomásra
- KÉTFÁZISÚ (round-trip keresés nélküli scraperekhez):
  1. ELŐSZŰRÉS: cheapestPerDay API-val (1 hívás / útvonal / irány)
  2. RÉSZLETES KERESÉS: csak a potenciális napokra

Az egynapos út a többnapos speciális esete 0 éjszakával.
"""

import logging
from datetime import date, timedelta
from typing import List, Set, Tuple, Optional
from itertools import product as cartesian_product

from models import Flight, DayTrip, SearchConfig
from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)


class FlightFilter:

    def __init__(self, config: SearchConfig, scrapers: List[BaseScraper]):
        self.config = config
        self.scrapers = scrapers
        # Lekérdezés-statisztika (a hívó ebből látja, ha hiányos az eredmény)
        self.total_requests = 0
        self.failed_requests = 0

    def find_trips(
        self,
        destinations: List[str] = None,
        dates: List[date] = None,
    ) -> List[DayTrip]:
        """
        Belépési pont. A round-trip keresést támogató scraperek a gyors úton mennek
        (egy kérés / nap), a többiek a kétfázisú (előszűrés + részletes) keresésen.
        """
        fast = [s for s in self.scrapers if s.supports_round_trip_search]
        slow = [s for s in self.scrapers if not s.supports_round_trip_search]

        trips: List[DayTrip] = []
        if fast:
            trips.extend(self._find_trips_fast(fast, destinations, dates))
        if slow:
            slow_filter = self if not fast else FlightFilter(self.config, slow)
            trips.extend(slow_filter._find_trips_two_phase(destinations, dates))
            if slow_filter is not self:
                self.total_requests += slow_filter.total_requests
                self.failed_requests += slow_filter.failed_requests

        trips.sort(key=lambda t: (
            t.trip_date, t.return_date,
            t.total_price if t.total_price is not None else float("inf"),
        ))
        return trips

    def _find_trips_fast(
        self,
        scrapers: List[BaseScraper],
        destinations: Optional[List[str]],
        dates: Optional[List[date]],
    ) -> List[DayTrip]:
        """Gyors keresés: (oda nap, vissza nap) páronként egy kérés, minden célállomásra."""
        if dates is None:
            dates = self._default_dates()

        date_pairs = [
            (d_out, d_out + timedelta(days=n))
            for d_out in sorted(dates) for n in self._night_range()
        ]
        # Üres lista = nincs szűrés (pl. ha a célállomás-lista lekérése nem sikerült)
        allowed = set(destinations) if destinations else None

        total = len(date_pairs)
        print(f"Keresés (roundTripFares): {total} nap-kombináció...", flush=True)

        all_trips: List[DayTrip] = []
        for idx, (d_out, d_back) in enumerate(date_pairs, 1):
            label = self._date_label(d_out, d_back)
            for scraper in scrapers:
                self.total_requests += 1
                try:
                    trips = scraper.search_round_trips(
                        origin=self.config.origin,
                        out_date=d_out,
                        back_date=d_back,
                        before_hour=self.config.morning_before,
                        after_hour=self.config.evening_after,
                        max_price=self.config.max_price,
                        destinations=destinations or None,
                    )
                except Exception as e:
                    scraper.take_sub_request_stats()
                    self._record_failure(f"{label} ({scraper.source_name})", e)
                    continue

                # Több kérést küldő scraper (pl. Wizz Air) a saját kérés-statisztikáját adja
                sub_total, sub_failed = scraper.take_sub_request_stats()
                if sub_total:
                    self.total_requests += sub_total - 1
                    self.failed_requests += sub_failed

                trips = [t for t in trips if self._is_wanted(t, allowed)]
                all_trips.extend(trips)
                if trips:
                    print(f"  [{idx}/{total}] {label}: {len(trips)} pár", flush=True)

        return all_trips

    def _record_failure(self, what: str, error: Exception) -> None:
        self.failed_requests += 1
        logger.error(f"Sikertelen lekérdezés – {what}: {error}")

    def _is_wanted(self, trip: DayTrip, allowed: Optional[Set[str]]) -> bool:
        if allowed is not None and trip.outbound.destination not in allowed:
            return False
        if self.config.max_price is not None and trip.total_price is not None:
            if trip.total_price > self.config.max_price:
                return False
        return True

    def _default_dates(self) -> List[date]:
        today = date.today()
        return [today + timedelta(days=i) for i in range(1, self.config.search_days + 1)]

    def _night_range(self) -> range:
        """Éjszakák száma az úton: egynapos módban csak 0."""
        if self.config.trip_mode == "multiday":
            return range(self.config.min_nights, self.config.max_nights + 1)
        return range(0, 1)

    @staticmethod
    def _date_label(d_out: date, d_back: date) -> str:
        return f"{d_out}" if d_back == d_out else f"{d_out}→{d_back}"

    # ── Kétfázisú keresés ──

    def _find_trips_two_phase(
        self,
        destinations: List[str] = None,
        dates: List[date] = None,
    ) -> List[DayTrip]:
        """Előszűrés + részletes keresés (round-trip keresés nélküli scraperekhez)."""
        if dates is None:
            dates = self._default_dates()

        if destinations is None:
            destinations = self._collect_all_destinations()

        if not destinations:
            return []

        # 1. FÁZIS: Előszűrés
        print("Előszűrés (cheapestPerDay)...", flush=True)
        candidates = self._prefilter_candidates(destinations, dates)

        if not candidates:
            print("  Nincs potenciális oda-vissza kombináció.")
            return []

        # 2. FÁZIS: Részletes keresés
        total = len(candidates)
        print(f"Részletes keresés: {total} kombináció...", flush=True)

        all_trips = []
        for idx, (dest, d_out, d_back) in enumerate(candidates, 1):
            label = f"{dest} {self._date_label(d_out, d_back)}"
            try:
                trips = self._search_destination_dates(dest, d_out, d_back)
            except Exception as e:
                self._record_failure(label, e)
                continue
            all_trips.extend(trips)
            if trips:
                print(f"  [{idx}/{total}] {label}: {len(trips)} pár", flush=True)

        return all_trips

    def _prefilter_candidates(
        self,
        destinations: List[str],
        dates: List[date],
    ) -> List[Tuple[str, date, date]]:
        """(célállomás, oda nap, vissza nap) hármasok, ahol mindkét irányban van járat."""
        night_range = self._night_range()
        date_from = min(dates)
        date_to = max(dates)
        date_set = set(dates)                                   # megengedett INDULÓ napok
        inbound_to = date_to + timedelta(days=night_range[-1])  # a visszaút túlnyúlhat az induló ablakon

        candidates: List[Tuple[str, date, date]] = []
        relevant = 0
        total = len(destinations)

        for idx, dest in enumerate(destinations, 1):
            print(f"\r  Előszűrés: {idx}/{total} ({dest})   ", end="", flush=True)

            outbound_dates = self._get_dates_with_flights(
                self.config.origin, dest, date_from, date_to
            )
            inbound_dates = self._get_dates_with_flights(
                dest, self.config.origin, date_from, inbound_to
            )

            before = len(candidates)
            for d_out in sorted(outbound_dates & date_set):
                for n in night_range:
                    d_back = d_out + timedelta(days=n)
                    if d_back in inbound_dates:
                        candidates.append((dest, d_out, d_back))

            if len(candidates) > before:
                relevant += 1

        print(f"\r  Előszűrés kész: {relevant}/{total} célállomás releváns, "
              f"{len(candidates)} nap-kombináció", flush=True)
        return candidates

    def _get_dates_with_flights(
        self, origin: str, destination: str, date_from: date, date_to: date
    ) -> Set[date]:
        dates_with_flights = set()
        for scraper in self.scrapers:
            self.total_requests += 1
            try:
                cheapest = scraper.get_cheapest_per_day(origin, destination, date_from, date_to)
            except Exception as e:
                self._record_failure(f"előszűrés {origin}→{destination} ({scraper.source_name})", e)
                continue
            for date_str, price in cheapest.items():
                if price is not None and price > 0:
                    try:
                        dates_with_flights.add(date.fromisoformat(date_str))
                    except ValueError:
                        pass
        return dates_with_flights

    def _collect_all_destinations(self) -> List[str]:
        all_dests = set()
        for scraper in self.scrapers:
            try:
                dests = scraper.get_destinations(self.config.origin)
                all_dests.update(dests)
            except Exception as e:
                logger.error(f"Célállomások lekérése sikertelen ({scraper.source_name}): {e}")
        return sorted(all_dests)

    def _search_destination_dates(
        self, destination: str, d_out: date, d_back: date
    ) -> List[DayTrip]:
        all_outbound: List[Flight] = []
        all_inbound: List[Flight] = []

        for scraper in self.scrapers:
            self.total_requests += 1
            try:
                all_outbound.extend(scraper.search_outbound_flights(
                    origin=self.config.origin,
                    destination=destination,
                    flight_date=d_out,
                    before_hour=self.config.morning_before,
                ))
            except Exception as e:
                self._record_failure(f"oda {destination} {d_out} ({scraper.source_name})", e)

            self.total_requests += 1
            try:
                all_inbound.extend(scraper.search_return_flights(
                    origin=destination,
                    destination=self.config.origin,
                    flight_date=d_back,
                    after_hour=self.config.evening_after,
                ))
            except Exception as e:
                self._record_failure(f"vissza {destination} {d_back} ({scraper.source_name})", e)

        trips = []
        for outbound, inbound in cartesian_product(all_outbound, all_inbound):
            trip = DayTrip(
                outbound=outbound,
                inbound=inbound,
                trip_date=d_out,
                return_date=d_back,
            )
            if self._is_wanted(trip, allowed=None):
                trips.append(trip)

        return trips
