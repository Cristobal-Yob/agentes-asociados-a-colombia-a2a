import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest


def credentials():
    from agenda_agent.calendar_auth import SCOPES

    return {
        "type": "authorized_user",
        "project_id": "example-project",
        "client_id": "test.apps.googleusercontent.com",
        "client_secret": "TEST_SECRET",
        "refresh_token": "TEST_REFRESH",
        "account_email": "sender@example.com",
        "account_sub": "sub",
        "scopes": sorted(SCOPES),
        "token_uri": "https://oauth2.googleapis.com/token",
    }


def test_calendar_oauth_reuses_state_nonce_pkce_but_not_gmail_scope():
    from agenda_agent.calendar_auth import SCOPES, validate_credentials
    from scripts.authorize_calendar import OAuthFlow

    values = credentials()
    validate_credentials(values)
    flow = OAuthFlow(values, "http://127.0.0.1:12345/callback")
    query = parse_qs(urlsplit(flow.authorization_url()).query)
    assert set(query["scope"][0].split()) == SCOPES
    assert query["code_challenge_method"] == ["S256"]
    assert query["login_hint"] == ["sender@example.com"]
    assert flow.deadline - flow.clock() > 1790
    values["scopes"] = ["openid", "email", "https://www.googleapis.com/auth/gmail.send"]
    with pytest.raises(ValueError):
        validate_credentials(values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_email", "other@example.com"),
        ("project_id", "other"),
        ("token_uri", "https://evil.invalid"),
        ("scopes", ["https://www.googleapis.com/auth/calendar"]),
    ],
)
def test_calendar_grant_is_exact_identity_project_and_scope(field, value):
    from agenda_agent.calendar_auth import validate_credentials

    data = credentials()
    data[field] = value
    with pytest.raises(ValueError):
        validate_credentials(data)


async def test_events_list_pagination_all_day_and_transparency(tmp_path):
    from agenda_agent.calendar import GoogleCalendar

    grant = tmp_path / "calendar.json"
    grant.write_text(json.dumps(credentials()))
    calls = []

    def respond(req):
        calls.append(req)
        if req.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "scope": " ".join(credentials()["scopes"]),
                    "token_type": "Bearer",
                    "access_token": "TEST_ACCESS",
                },
            )
        if req.url.path.endswith("userinfo"):
            return httpx.Response(
                200,
                json={"email": "sender@example.com", "email_verified": True, "sub": "sub"},
            )
        assert req.method == "GET" and req.url.path.endswith("/calendars/primary/events")
        assert req.url.params["singleEvents"] == "true"
        if "pageToken" not in req.url.params:
            assert "timeZone" not in req.url.params
            return httpx.Response(
                200,
                json={
                    "timeZone": "America/Bogota",
                    "nextPageToken": "next",
                    "items": [
                        {"start": {"date": "2030-05-06"}, "end": {"date": "2030-05-07"}},
                        {"status": "cancelled"},
                        {"transparency": "transparent"},
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "start": {"dateTime": "2030-05-07T10:00:00-05:00"},
                        "end": {"dateTime": "2030-05-07T11:00:00-05:00"},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        calendar = GoogleCalendar(str(grant), client=client)
        busy = await calendar.availability(
            datetime(2030, 5, 6, tzinfo=UTC), datetime(2030, 5, 8, tzinfo=UTC)
        )
    assert len(busy) == 2
    assert busy[0][0] == datetime(2030, 5, 6, 5, tzinfo=UTC)
    assert len([req for req in calls if req.url.path.endswith("/events")]) == 2


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_calendar_http_errors_do_not_fallback(tmp_path, status):
    from agenda_agent.calendar import CalendarError, GoogleCalendar

    grant = tmp_path / "calendar.json"
    grant.write_text(json.dumps(credentials()))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(status, json={}))
    ) as client:
        calendar = GoogleCalendar(str(grant), client=client)
        with pytest.raises(CalendarError):
            await calendar.availability(
                datetime(2030, 5, 6, tzinfo=UTC), datetime(2030, 5, 8, tzinfo=UTC)
            )


def test_calendar_cli_writes_separate_protected_key_without_overwrite(tmp_path, monkeypatch):
    from email_agent import gmail_auth
    from scripts import authorize_calendar

    monkeypatch.setattr(gmail_auth, "require_live_identity", lambda: None)

    grant = tmp_path / "calendar.json"
    key = tmp_path / "agenda-key.txt"

    class Flow:
        def exchange(self, code, client):
            return credentials()

    monkeypatch.setattr(authorize_calendar, "load_client", lambda path: credentials())
    monkeypatch.setattr(
        authorize_calendar, "receive_callback", lambda config: (Flow(), "TEST_CODE")
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "authorize_calendar",
            "--client",
            "TEST_CLIENT",
            "--output",
            str(grant),
            "--signing-key-output",
            str(key),
        ],
    )
    authorize_calendar.main()
    assert grant.is_file() and len(key.read_text()) >= 32
    saved = key.read_text()
    with pytest.raises(SystemExit):
        authorize_calendar.main()
    assert key.read_text() == saved


async def test_calendar_insert_contract_and_get_readback_are_bounded(tmp_path):
    from agenda_agent.calendar import GoogleCalendar
    from agenda_agent.contracts import AgendaEvent

    grant = tmp_path / "calendar.json"
    grant.write_text(json.dumps(credentials()))
    event_id = "a" * 64
    calls = []

    def respond(req):
        if req.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "scope": " ".join(credentials()["scopes"]),
                    "token_type": "Bearer",
                    "access_token": "TEST_ACCESS",
                },
            )
        if req.url.path.endswith("userinfo"):
            return httpx.Response(
                200,
                json={"email": "sender@example.com", "email_verified": True, "sub": "sub"},
            )
        calls.append(req)
        if req.method == "POST":
            data = json.loads(req.content)
            assert req.url.params["sendUpdates"] == "none"
            assert data["id"] == event_id and data["attendees"] == []
            assert data["reminders"] == {"useDefault": False}
            assert data["extendedProperties"]["private"]["agendaPlan"] == "plan"
            return httpx.Response(200, json=data)
        assert req.url.path.endswith("/" + event_id)
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        calendar = GoogleCalendar(str(grant), client=client)
        event = AgendaEvent(
            title="Repaso",
            start=datetime(2030, 5, 6, 15, tzinfo=UTC),
            end=datetime(2030, 5, 6, 16, tzinfo=UTC),
            timezone="America/Bogota",
        )
        assert (await calendar.create(event_id, event, "plan"))["id"] == event_id
        assert await calendar.get(event_id) is None
    assert len(calls) == 2


@pytest.mark.parametrize(
    "failure", ["timeout", "pages", "all_day_timezone_missing", "provider_403"]
)
async def test_calendar_incomplete_availability_is_never_assumed_free(tmp_path, failure):
    from agenda_agent.calendar import CalendarError, GoogleCalendar

    grant = tmp_path / "calendar.json"
    grant.write_text(json.dumps(credentials()))
    count = 0

    def respond(req):
        nonlocal count
        if req.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "scope": " ".join(credentials()["scopes"]),
                    "token_type": "Bearer",
                    "access_token": "TEST_ACCESS",
                },
            )
        if req.url.path.endswith("userinfo"):
            return httpx.Response(
                200,
                json={"email": "sender@example.com", "email_verified": True, "sub": "sub"},
            )
        count += 1
        assert req.method == "GET"
        if failure == "timeout":
            raise httpx.ReadTimeout("TEST")
        if failure == "provider_403":
            return httpx.Response(403, json={})
        if failure == "pages":
            return httpx.Response(200, json={"nextPageToken": "next", "items": []})
        return httpx.Response(
            200, json={"items": [{"start": {"date": "2030-05-06"}, "end": {"date": "2030-05-07"}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(CalendarError):
            await GoogleCalendar(str(grant), client=client).availability(
                datetime(2030, 5, 6, tzinfo=UTC), datetime(2030, 5, 8, tzinfo=UTC)
            )
    assert count == (5 if failure == "pages" else 1)
