"""Explicit production configuration; never load credentials from the checkout."""

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

OWNER_EMAIL = "owner@example.com"
HUB_RETURN = "https://hub.example.invalid/classroom"
ITINERARY_URL = "https://itinerary.example.invalid"
EMAIL_URL = "https://email.example.invalid"


def valid_origin(origin, *, allow_loopback=False):
    try:
        parsed = urlsplit(origin)
        loopback = (
            allow_loopback
            and parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
        )
        hostname = parsed.hostname or ""
        valid_host = bool(re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", hostname))
        return bool(
            (parsed.scheme == "https" or loopback)
            and valid_host
            and len(hostname) <= 253
            and not parsed.username
            and not parsed.password
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
            and parsed.port != 0
            and (loopback or parsed.port in {None, 443})
            and origin == parsed.scheme + "://" + parsed.netloc
        )
    except ValueError:
        return False


@dataclass(frozen=True)
class Config:
    origin: str = ""
    client_id: str = ""
    client_secret: str = ""
    project: str = ""
    database: str = ""
    itinerary_key: str = ""
    email_key: str = ""
    owner_email: str = ""
    itinerary_url: str = ""
    email_url: str = ""
    hub_return: str = ""
    sender_email: str = ""

    ENV_NAMES = (
        "COLOMBIA_UI_ORIGIN",
        "COLOMBIA_UI_GOOGLE_CLIENT_ID",
        "COLOMBIA_UI_GOOGLE_CLIENT_SECRET",
        "COLOMBIA_UI_FIRESTORE_PROJECT",
        "COLOMBIA_UI_FIRESTORE_DATABASE",
        "COLOMBIA_UI_ITINERARY_KEY",
        "COLOMBIA_UI_EMAIL_KEY",
        "COLOMBIA_UI_ALLOWED_EMAIL",
        "COLOMBIA_UI_ITINERARY_URL",
        "COLOMBIA_UI_EMAIL_URL",
        "COLOMBIA_UI_HUB_RETURN",
        "COLOMBIA_UI_SENDER_EMAIL",
    )

    @classmethod
    def from_env(cls):
        return cls(*(os.environ.get(name, "") for name in cls.ENV_NAMES))

    @property
    def secure(self):
        return self.origin.startswith("https://")

    @property
    def session_cookie(self):
        return "__Host-colombia-session" if self.secure else "colombia-local-session"

    @property
    def login_cookie(self):
        return "__Host-colombia-login" if self.secure else "colombia-local-login"

    @property
    def ready(self):
        try:
            return bool(
                valid_origin(self.origin, allow_loopback=True)
                and self.client_id.endswith(".apps.googleusercontent.com")
                and self.client_secret
                and re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", self.project)
                and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", self.owner_email)
                and self.owner_email == self.owner_email.casefold()
                and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", self.sender_email)
                and valid_origin(self.itinerary_url, allow_loopback=True)
                and valid_origin(self.email_url, allow_loopback=True)
                and self.itinerary_url != self.email_url
                and valid_origin(
                    urlsplit(self.hub_return).scheme + "://" + urlsplit(self.hub_return).netloc
                )
                and not urlsplit(self.hub_return).query
                and self.database
                and self.database != "(default)"
                and self.database not in {"colombia-a2a-email", "colombia-a2a-itinerary"}
                and len(self.itinerary_key) >= 32
                and len(self.email_key) >= 32
            )
        except ValueError:
            return False
