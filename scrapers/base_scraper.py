"""
Absztrakt alap osztály az összes légitársasági scraper számára.
"""

import logging
from abc import ABC, abstractmethod
from datetime import date
from typing import List, Optional, Dict

from models import Flight, Airline

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """Közös interface minden légitársasági scraperhez."""

    def __init__(self, airline: Airline, source_name: str, currency: str = "EUR"):
        self.airline = airline
        self.source_name = source_name
        self.currency = currency
        self.logger = logging.getLogger(f"scraper.{source_name}")

    @abstractmethod
    def get_destinations(self, origin: str) -> List[str]:
        pass

    @abstractmethod
    def search_flights(
        self,
        origin: str,
        destination: str,
        flight_date: date,
        departure_time_from: Optional[str] = None,
        departure_time_to: Optional[str] = None,
    ) -> List[Flight]:
        pass

    def get_cheapest_per_day(
        self, origin: str, destination: str, date_from: date, date_to: date
    ) -> Dict[str, Optional[float]]:
        """
        Napi legolcsóbb ár lekérése. Alap implementáció: üres dict.
        A scraperek felülírhatják ha van ilyen API-juk.
        """
        return {}

    def search_outbound_flights(
        self, origin: str, destination: str, flight_date: date, before_hour: int = 9
    ) -> List[Flight]:
        flights = self.search_flights(
            origin=origin,
            destination=destination,
            flight_date=flight_date,
            departure_time_from="00:00",
            departure_time_to=f"{before_hour - 1:02d}:59",
        )
        return [f for f in flights if f.is_morning_departure(before_hour)]

    def search_return_flights(
        self, origin: str, destination: str, flight_date: date, after_hour: int = 18
    ) -> List[Flight]:
        flights = self.search_flights(
            origin=origin,
            destination=destination,
            flight_date=flight_date,
            departure_time_from=f"{after_hour:02d}:00",
            departure_time_to="23:59",
        )
        return [f for f in flights if f.is_evening_departure(after_hour)]