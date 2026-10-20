import hashlib
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from agenda_agent.calendar_auth import ACCOUNT, authorize_runtime, load_credentials
from agenda_agent.signing import canonical

BASE_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


class CalendarError(ValueError):
    pass


class GoogleCalendar:
    account = ACCOUNT

    def __init__(self, path, client=None):
        self.credentials = load_credentials(path)
        self.client = client
        self.fingerprint = hashlib.sha256(canonical(self.credentials)).hexdigest()

    async def request(self, method, suffix="", **kwargs):
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=4, follow_redirects=False)
        try:
            headers = await authorize_runtime(self.credentials, client)
            response = await client.request(
                method, BASE_URL + suffix, headers=headers, timeout=4, **kwargs
            )
            if method == "GET" and suffix and response.status_code == 404:
                return None
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("calendar_response_invalid")
            return data
        except Exception:
            raise CalendarError(
                "Calendar no respondió de forma verificable; no se simuló el resultado."
            ) from None
        finally:
            if owned:
                await client.aclose()

    async def availability(self, start, end):
        intervals = []
        token = None
        for _ in range(5):
            params = {
                "timeMin": start.astimezone(UTC).isoformat(),
                "timeMax": end.astimezone(UTC).isoformat(),
                "singleEvents": "true",
                "maxResults": "250",
            }
            if token:
                params["pageToken"] = token
            data = await self.request("GET", params=params)
            try:
                for event in data.get("items", []):
                    if (
                        event.get("status") == "cancelled"
                        or event.get("transparency") == "transparent"
                    ):
                        continue
                    values = []
                    for key in ("start", "end"):
                        value = event[key]
                        if "dateTime" in value:
                            instant = datetime.fromisoformat(value["dateTime"])
                            if instant.tzinfo is None:
                                raise ValueError("calendar_timezone_missing")
                        else:
                            instant = datetime.fromisoformat(value["date"]).replace(
                                tzinfo=ZoneInfo(value.get("timeZone") or data["timeZone"])
                            )
                        values.append(instant.astimezone(UTC))
                    if values[0] >= values[1]:
                        raise ValueError("calendar_interval_invalid")
                    intervals.append(tuple(values))
                token = data.get("nextPageToken")
                if not token:
                    return intervals
            except (KeyError, TypeError, ValueError):
                raise CalendarError("Calendar devolvió intervalos no válidos.") from None
        raise CalendarError(
            "La consulta de disponibilidad superó el límite de páginas; no se "
            "asumió disponibilidad."
        )

    async def create(self, event_id, event, plan_id):
        return await self.request(
            "POST",
            params={"sendUpdates": "none"},
            json={
                "id": event_id,
                "summary": event.title,
                "start": {"dateTime": event.start.isoformat(), "timeZone": event.timezone},
                "end": {"dateTime": event.end.isoformat(), "timeZone": event.timezone},
                "attendees": [],
                "reminders": {"useDefault": False},
                "extendedProperties": {"private": {"agendaPlan": plan_id}},
            },
        )

    async def get(self, event_id):
        if len(event_id) != 64 or any(char not in "0123456789abcdef" for char in event_id):
            raise CalendarError("Identificador de evento no válido.")
        return await self.request("GET", "/" + event_id)
