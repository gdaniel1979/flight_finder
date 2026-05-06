"""
Brevo email értesítő modul.

Három stratégiát próbál:
1. brevo-python v4 SDK (legújabb)
2. sib-api-v3-sdk (régi, de elterjedt)
3. Közvetlen HTTP hívás a Brevo API-ra (mindig működik)

Használat:
    notifier = EmailNotifier(
        api_key="xkeysib-...",
        sender_email="me@example.com",
        sender_name="Flight Finder",
        recipient_emails=["you@example.com"],
    )
    notifier.send_day_trips(trips)
"""

import json
import logging
from collections import defaultdict
from datetime import datetime
from typing import List, Optional

import requests

from models import DayTrip

logger = logging.getLogger(__name__)


def _check_brevo_v4() -> bool:
    try:
        from brevo import Brevo
        return True
    except Exception:
        return False


def _check_sib_v3() -> bool:
    try:
        import sib_api_v3_sdk
        return True
    except Exception:
        return False


class EmailNotifier:
    """Brevo email értesítő."""

    def __init__(
        self,
        api_key: str,
        sender_email: str,
        recipient_emails: List[str],
        sender_name: str = "Ryanair Finder",
    ):
        self.api_key = api_key
        self.sender_email = sender_email
        self.sender_name = sender_name
        self.recipient_emails = recipient_emails
        self.logger = logging.getLogger("email")

        self._brevo_v4 = _check_brevo_v4()
        self._sib_v3 = _check_sib_v3()

        if self._brevo_v4:
            self.logger.info("Email: brevo-python v4 SDK")
        elif self._sib_v3:
            self.logger.info("Email: sib-api-v3-sdk")
        else:
            self.logger.info("Email: közvetlen HTTP API")

    def send_day_trips(self, trips: List[DayTrip]) -> bool:
        """
        Járatpárok küldése emailben.

        Returns:
            True ha sikeres, False ha nem
        """
        if not trips:
            self.logger.info("Nincs járatpár – email nem kerül kiküldésre")
            return True

        subject = self._build_subject(trips)
        html = self._build_html(trips)

        return self._send_email(subject, html)

    def _send_email(self, subject: str, html_content: str) -> bool:
        """Email küldése a rendelkezésre álló módszerrel."""
        if self._brevo_v4:
            try:
                return self._send_via_brevo_v4(subject, html_content)
            except Exception as e:
                self.logger.warning(f"brevo v4 hiba, fallback: {e}")

        if self._sib_v3:
            try:
                return self._send_via_sib_v3(subject, html_content)
            except Exception as e:
                self.logger.warning(f"sib v3 hiba, fallback: {e}")

        return self._send_via_http(subject, html_content)

    # ── brevo-python v4 ──

    def _send_via_brevo_v4(self, subject: str, html_content: str) -> bool:
        from brevo import Brevo
        from brevo.transactional_emails import (
            SendTransacEmailRequestSender,
            SendTransacEmailRequestToItem,
        )

        client = Brevo(api_key=self.api_key)
        client.transactional_emails.send_transac_email(
            sender=SendTransacEmailRequestSender(
                email=self.sender_email,
                name=self.sender_name,
            ),
            to=[SendTransacEmailRequestToItem(email=e) for e in self.recipient_emails],
            subject=subject,
            html_content=html_content,
        )
        self.logger.info(f"Email elküldve (brevo v4) → {', '.join(self.recipient_emails)}")
        return True

    # ── sib-api-v3-sdk ──

    def _send_via_sib_v3(self, subject: str, html_content: str) -> bool:
        import sib_api_v3_sdk
        from sib_api_v3_sdk.rest import ApiException

        configuration = sib_api_v3_sdk.Configuration()
        configuration.api_key["api-key"] = self.api_key
        api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
            sib_api_v3_sdk.ApiClient(configuration)
        )
        email = sib_api_v3_sdk.SendSmtpEmail(
            sender={"email": self.sender_email, "name": self.sender_name},
            to=[{"email": e} for e in self.recipient_emails],
            subject=subject,
            html_content=html_content,
        )
        api_instance.send_transac_email(email)
        self.logger.info(f"Email elküldve (sib v3) → {', '.join(self.recipient_emails)}")
        return True

    # ── Közvetlen HTTP ──

    def _send_via_http(self, subject: str, html_content: str) -> bool:
        url = "https://api.brevo.com/v3/smtp/email"
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "api-key": self.api_key,
        }
        payload = {
            "sender": {"name": self.sender_name, "email": self.sender_email},
            "to": [{"email": e} for e in self.recipient_emails],
            "subject": subject,
            "htmlContent": html_content,
        }

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            self.logger.info(f"Email elküldve (HTTP) → {', '.join(self.recipient_emails)}")
            return True
        except requests.RequestException as e:
            self.logger.error(f"Email küldés sikertelen: {e}")
            if hasattr(e, "response") and e.response is not None:
                self.logger.error(f"Válasz: {e.response.text[:500]}")
            return False

    # ── Email tartalom ──

    def _build_subject(self, trips: List[DayTrip]) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        cheapest = min(
            (t for t in trips if t.total_price is not None),
            key=lambda t: t.total_price,
            default=None,
        )
        if cheapest:
            return (
                f"Ryanair Finder - {len(trips)} járatpár - "
                f"Legjobb: {cheapest.outbound.destination_city or cheapest.outbound.destination} "
                f"{cheapest.total_price:.0f} {cheapest.outbound.currency}"
            )
        return f"Ryanair Finder - {len(trips)} járatpár találva"

    def _build_html(self, trips: List[DayTrip]) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        # Csoportosítás célállomás szerint. Kulcs: (város, reptér_kód)
        # A város az elsődleges rendezési szempont, a reptér_kód csak disambiguáló.
        groups: dict = defaultdict(list)
        for trip in trips:
            o = trip.outbound
            city = o.destination_city or "?"
            airport = o.destination
            groups[(city, airport)].append(trip)

        # Célvárosok ábécé sorrendben (case-insensitive)
        sorted_keys = sorted(groups.keys(), key=lambda k: (k[0].lower(), k[1]))

        sections = ""
        for city, airport in sorted_keys:
            group_trips = groups[(city, airport)]

            # Dátum szerint ascending
            group_trips_sorted = sorted(group_trips, key=lambda t: t.trip_date)

            # Legolcsóbb ár megkeresése (None-ok kizárva)
            priced = [t for t in group_trips_sorted if t.total_price is not None]
            min_price = min((t.total_price for t in priced), default=None)

            rows = ""
            for trip in group_trips_sorted:
                o = trip.outbound
                i = trip.inbound
                price_str = (
                    f"{trip.total_price:.2f} {o.currency}"
                    if trip.total_price is not None
                    else "N/A"
                )
                # Holtverseny esetén minden legolcsóbb sor zöld; N/A sor soha nem zöld
                is_cheapest = (
                    min_price is not None
                    and trip.total_price is not None
                    and trip.total_price == min_price
                )
                row_style = (
                    "color:#1b8a3a;font-weight:bold;" if is_cheapest else "color:#333;"
                )

                rows += f"""
                <tr style="{row_style}">
                    <td style="padding:8px;border-bottom:1px solid #eee;">{trip.trip_date}</td>
                    <td style="padding:8px;border-bottom:1px solid #eee;">{o.flight_number or '?'}<br>{o.departure_time.strftime('%H:%M')}</td>
                    <td style="padding:8px;border-bottom:1px solid #eee;">{i.flight_number or '?'}<br>{i.departure_time.strftime('%H:%M')}</td>
                    <td style="padding:8px;border-bottom:1px solid #eee;">{price_str}</td>
                </tr>"""

            sections += f"""
            <div style="margin:32px 0;padding:16px;background:#fafafa;border-left:4px solid #1a73e8;border-radius:4px;">
                <h3 style="margin:0 0 12px 0;color:#1a73e8;font-size:18px;">
                    {city} <span style="color:#666;font-weight:normal;">({airport})</span>
                </h3>
                <table style="width:100%;border-collapse:collapse;font-size:14px;background:#fff;">
                    <thead>
                        <tr style="background:#f0f0f0;">
                            <th style="padding:8px;text-align:left;">Dátum</th>
                            <th style="padding:8px;text-align:left;">Oda</th>
                            <th style="padding:8px;text-align:left;">Vissza</th>
                            <th style="padding:8px;text-align:left;">Összár</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows}
                    </tbody>
                </table>
            </div>"""

        return f"""
        <html>
        <body style="font-family:Arial,sans-serif;color:#333;max-width:800px;margin:0 auto;padding:16px;">
            <h2 style="color:#1a73e8;">Napi járatpárok</h2>
            <p style="color:#666;">Generálva: {now} | Találatok: {len(trips)} | Célállomások: {len(sorted_keys)}</p>

            {sections}
        </body>
        </html>
        """
