"""
Szűrő és párosító modul – optimalizált verzió.

Két fázisban működik:
1. ELŐSZŰRÉS: cheapestPerDay API-val (1 hívás / útvonal / irány)
2. RÉSZLETES KERESÉS: csak a potenciális napokra
"""

import sys
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

    def find_trips(
        self,
        destinations: List[str] = None,
        dates: List[date] = None,
    ) -> List[DayTrip]:
        """Belépési pont: a trip_mode alapján egynapos vagy többnapos keresés."""
        if self.config.trip_mode == "multiday":
            return self.find_multiday_trips(destinations=destinations, dates=dates)
        return self.find_day_trips(destinations=destinations, dates=dates)

    def find_day_trips(
        self,
        destinations: List[str] = None,
        dates: List[date] = None,
    ) -> List[DayTrip]:

        if dates is None:
            today = date.today()
            dates = [today + timedelta(days=i) for i in range(1, self.config.search_days + 1)]

        if destinations is None:
            destinations = self._collect_all_destinations()

        if not destinations:
            return []

        date_from = min(dates)
        date_to = max(dates)
        date_set = set(dates)

        # 1. FÁZIS: Előszűrés
        print("Előszűrés (cheapestPerDay)...", flush=True)
        candidates = self._prefilter_candidates(destinations, date_from, date_to, date_set)

        if not candidates:
            print("  Nincs potenciális oda-vissza nap.")
            return []

        # 2. FÁZIS: Részletes keresés
        total = len(candidates)
        print(f"Részletes keresés: {total} kombináció...", flush=True)

        all_trips = []
        for idx, (dest, search_date) in enumerate(candidates, 1):
            try:
                day_trips = self._search_destination_date(dest, search_date)
                all_trips.extend(day_trips)
                if day_trips:
                    print(f"  [{idx}/{total}] {dest} {search_date}: {len(day_trips)} pár", flush=True)
            except Exception as e:
                logger.error(f"Hiba {dest} {search_date}: {e}")

        all_trips.sort(key=lambda t: (t.trip_date, t.total_price if t.total_price is not None else float("inf")))
        return all_trips

    def _prefilter_candidates(
        self,
        destinations: List[str],
        date_from: date,
        date_to: date,
        date_set: Set[date],
    ) -> List[Tuple[str, date]]:

        candidates = []
        relevant = 0
        total = len(destinations)

        for idx, dest in enumerate(destinations, 1):
            print(f"\r  Előszűrés: {idx}/{total} ({dest})   ", end="", flush=True)

            outbound_dates = self._get_dates_with_flights(
                self.config.origin, dest, date_from, date_to
            )
            inbound_dates = self._get_dates_with_flights(
                dest, self.config.origin, date_from, date_to
            )

            both_directions = outbound_dates & inbound_dates & date_set

            if both_directions:
                relevant += 1
                for d in sorted(both_directions):
                    candidates.append((dest, d))

        print(f"\r  Előszűrés kész: {relevant}/{total} célállomás releváns, "
              f"{len(candidates)} nap-kombináció", flush=True)
        return candidates

    def _get_dates_with_flights(
        self, origin: str, destination: str, date_from: date, date_to: date
    ) -> Set[date]:
        dates_with_flights = set()
        for scraper in self.scrapers:
            try:
                cheapest = scraper.get_cheapest_per_day(origin, destination, date_from, date_to)
                for date_str, price in cheapest.items():
                    if price is not None and price > 0:
                        try:
                            dates_with_flights.add(date.fromisoformat(date_str))
                        except ValueError:
                            pass
            except Exception:
                pass
        return dates_with_flights

    def _collect_all_destinations(self) -> List[str]:
        all_dests = set()
        for scraper in self.scrapers:
            try:
                dests = scraper.get_destinations(self.config.origin)
                all_dests.update(dests)
            except Exception:
                pass
        return sorted(all_dests)

    def _search_destination_date(self, destination: str, search_date: date) -> List[DayTrip]:
        all_outbound: List[Flight] = []
        all_inbound: List[Flight] = []

        for scraper in self.scrapers:
            try:
                all_outbound.extend(scraper.search_outbound_flights(
                    origin=self.config.origin,
                    destination=destination,
                    flight_date=search_date,
                    before_hour=self.config.morning_before,
                ))
            except Exception:
                pass

            try:
                all_inbound.extend(scraper.search_return_flights(
                    origin=destination,
                    destination=self.config.origin,
                    flight_date=search_date,
                    after_hour=self.config.evening_after,
                ))
            except Exception:
                pass

        if not all_outbound or not all_inbound:
            return []

        trips = []
        for outbound, inbound in cartesian_product(all_outbound, all_inbound):
            trip = DayTrip(
                outbound=outbound,
                inbound=inbound,
                trip_date=search_date,
            )
            if self.config.max_price is not None and trip.total_price is not None:
                if trip.total_price > self.config.max_price:
                    continue
            trips.append(trip)

        return trips

    def find_day_trips_single_date(self, search_date: date, destinations: List[str] = None) -> List[DayTrip]:
        return self.find_day_trips(destinations=destinations, dates=[search_date])

    # ── Többnapos keresés ──

    def find_multiday_trips(
        self,
        destinations: List[str] = None,
        dates: List[date] = None,
    ) -> List[DayTrip]:
        """Többnapos utak: reggeli oda az induló napon, esti vissza min_nights–max_nights éjszakával később."""
        if dates is None:
            today = date.today()
            dates = [today + timedelta(days=i) for i in range(1, self.config.search_days + 1)]

        if destinations is None:
            destinations = self._collect_all_destinations()

        if not destinations:
            return []

        min_n = self.config.min_nights
        max_n = self.config.max_nights

        date_from = min(dates)
        date_to = max(dates)
        date_set = set(dates)                          # megengedett INDULÓ napok
        inbound_to = date_to + timedelta(days=max_n)   # a visszaút túlnyúlhat az induló ablakon

        # 1. FÁZIS: Előszűrés
        print("Előszűrés (cheapestPerDay)...", flush=True)
        candidates = self._prefilter_multiday(
            destinations, date_from, date_to, inbound_to, date_set, min_n, max_n
        )

        if not candidates:
            print("  Nincs potenciális többnapos kombináció.")
            return []

        # 2. FÁZIS: Részletes keresés
        total = len(candidates)
        print(f"Részletes keresés: {total} kombináció...", flush=True)

        all_trips = []
        for idx, (dest, d_out, d_back) in enumerate(candidates, 1):
            try:
                trips = self._search_destination_dates(dest, d_out, d_back)
                all_trips.extend(trips)
                if trips:
                    print(f"  [{idx}/{total}] {dest} {d_out}→{d_back}: {len(trips)} pár", flush=True)
            except Exception as e:
                logger.error(f"Hiba {dest} {d_out}→{d_back}: {e}")

        all_trips.sort(key=lambda t: (
            t.trip_date, t.return_date,
            t.total_price if t.total_price is not None else float("inf"),
        ))
        return all_trips

    def _prefilter_multiday(
        self,
        destinations: List[str],
        date_from: date,
        date_to: date,
        inbound_to: date,
        date_set: Set[date],
        min_n: int,
        max_n: int,
    ) -> List[Tuple[str, date, date]]:

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

            dest_relevant = False
            for d_out in sorted(outbound_dates & date_set):
                for n in range(min_n, max_n + 1):
                    d_back = d_out + timedelta(days=n)
                    if d_back in inbound_dates:
                        candidates.append((dest, d_out, d_back))
                        dest_relevant = True

            if dest_relevant:
                relevant += 1

        print(f"\r  Előszűrés kész: {relevant}/{total} célállomás releváns, "
              f"{len(candidates)} nap-kombináció", flush=True)
        return candidates

    def _search_destination_dates(
        self, destination: str, d_out: date, d_back: date
    ) -> List[DayTrip]:
        all_outbound: List[Flight] = []
        all_inbound: List[Flight] = []

        for scraper in self.scrapers:
            try:
                all_outbound.extend(scraper.search_outbound_flights(
                    origin=self.config.origin,
                    destination=destination,
                    flight_date=d_out,
                    before_hour=self.config.morning_before,
                ))
            except Exception:
                pass

            try:
                all_inbound.extend(scraper.search_return_flights(
                    origin=destination,
                    destination=self.config.origin,
                    flight_date=d_back,
                    after_hour=self.config.evening_after,
                ))
            except Exception:
                pass

        if not all_outbound or not all_inbound:
            return []

        trips = []
        for outbound, inbound in cartesian_product(all_outbound, all_inbound):
            trip = DayTrip(
                outbound=outbound,
                inbound=inbound,
                trip_date=d_out,
                return_date=d_back,
            )
            if self.config.max_price is not None and trip.total_price is not None:
                if trip.total_price > self.config.max_price:
                    continue
            trips.append(trip)

        return trips