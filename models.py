"""
Közös adatmodellek a járatkereső rendszerhez.

Minden scraper modul ezeket a modelleket használja,
így az adatok egységes formátumban kerülnek a szűrőbe és a kimenetbe.
"""

from datetime import datetime, date, time
from typing import Optional
from pydantic import BaseModel, Field
from enum import Enum


class Airline(str, Enum):
    """Támogatott légitársaságok."""
    RYANAIR = "Ryanair"
    WIZZAIR = "Wizz Air"
    EASYJET = "easyJet"
    OTHER = "Other"


class Flight(BaseModel):
    """Egyetlen járat adatai."""

    airline: Airline
    flight_number: Optional[str] = None
    origin: str = Field(..., min_length=3, max_length=3, description="IATA kód (pl. BUD)")
    destination: str = Field(..., min_length=3, max_length=3, description="IATA kód (pl. BCN)")
    origin_city: Optional[str] = None
    destination_city: Optional[str] = None
    departure_time: datetime
    arrival_time: Optional[datetime] = None
    price: Optional[float] = None
    currency: str = "EUR"
    source: str = Field(..., description="Adatforrás neve (pl. 'ryanair-api', 'wizzair-scraper')")

    @property
    def departure_date(self) -> date:
        return self.departure_time.date()

    @property
    def departure_time_only(self) -> time:
        return self.departure_time.time()

    def is_morning_departure(self, before_hour: int = 9) -> bool:
        """Reggeli járat-e (adott óra előtt indul)."""
        return self.departure_time.hour < before_hour

    def is_evening_departure(self, after_hour: int = 18) -> bool:
        """Esti járat-e (adott óra után indul)."""
        return self.departure_time.hour >= after_hour


class DayTrip(BaseModel):
    """Egy oda-vissza járatpár.

    Egynapos útnál a vissza-dátum megegyezik az oda-dátummal (nights == 0);
    többnapos útnál a `return_date` később van, mint a `trip_date`.
    """

    outbound: Flight
    inbound: Flight
    total_price: Optional[float] = None
    trip_date: date                       # oda (indulás) napja
    return_date: Optional[date] = None    # vissza napja; None → egynapos (== trip_date)

    def model_post_init(self, __context) -> None:
        if self.return_date is None:
            self.return_date = self.trip_date
        if self.total_price is None and self.outbound.price and self.inbound.price:
            self.total_price = self.outbound.price + self.inbound.price

    @property
    def nights(self) -> int:
        """Éjszakák száma az úton (egynapos útnál 0)."""
        return (self.return_date - self.trip_date).days

    def summary(self) -> str:
        """Rövid összefoglaló a járatpárról."""
        out_time = self.outbound.departure_time.strftime("%H:%M")
        in_time = self.inbound.departure_time.strftime("%H:%M")
        price_str = f"{self.total_price:.2f} {self.outbound.currency}" if self.total_price else "N/A"
        if self.nights > 0:
            date_str = f"{self.trip_date} → {self.return_date} ({self.nights} éj)"
        else:
            date_str = f"{self.trip_date}"
        return (
            f"{date_str} | "
            f"{self.outbound.origin} → {self.outbound.destination} ({self.outbound.destination_city or '?'}) | "
            f"Oda: {out_time} | Vissza: {in_time} | "
            f"Ár: {price_str} | "
            f"{self.outbound.airline.value}"
        )


class SearchConfig(BaseModel):
    """Keresési konfiguráció."""

    origin: str = "BUD"
    morning_before: int = Field(default=9, ge=0, le=12, description="Reggeli határ (óra)")
    evening_after: int = Field(default=18, ge=12, le=23, description="Esti határ (óra)")
    search_days: int = Field(default=30, ge=1, le=90, description="Hány napra előre keresünk")
    currency: str = "EUR"
    max_price: Optional[float] = None
    trip_mode: str = Field(default="daytrip", description="'daytrip' (egynapos) vagy 'multiday' (többnapos)")
    min_nights: int = Field(default=2, ge=1, le=30, description="Többnapos: legkevesebb éjszaka")
    max_nights: int = Field(default=4, ge=1, le=30, description="Többnapos: legtöbb éjszaka")
