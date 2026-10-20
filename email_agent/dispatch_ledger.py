import asyncio
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx


class LedgerError(ValueError):
    pass


@dataclass
class Claim:
    key: str
    new: bool
    status: str = "reserved"
    provider_id: str | None = None
    generation: str | None = None


class SqliteLedger:
    def __init__(self, path):
        self.path = Path(path).resolve()
        project = Path(__file__).resolve().parents[1]
        if self.path.is_relative_to(project):
            raise ValueError("El registro SQLite de envíos debe estar fuera del proyecto")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS dispatch ("
                "key TEXT PRIMARY KEY, status TEXT NOT NULL, provider_id TEXT, time REAL NOT NULL)"
            )

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _reserve(self, key):
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise LedgerError("ledger_key_invalid")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "INSERT OR IGNORE INTO dispatch VALUES (?, 'reserved', NULL, ?)",
                (key, time.time()),
            )
            new = row.rowcount == 1
            status, provider_id = db.execute(
                "SELECT status, provider_id FROM dispatch WHERE key=?", (key,)
            ).fetchone()
            return Claim(key, new, status, provider_id)

    async def reserve(self, key):
        return await asyncio.to_thread(self._reserve, key)

    def _complete(self, claim, status, provider_id):
        with self._connection() as db:
            row = db.execute(
                "UPDATE dispatch SET status=?, provider_id=?, time=? "
                "WHERE key=? AND status='reserved'",
                (status, provider_id, time.time(), claim.key),
            )
            return row.rowcount == 1

    async def complete(self, claim, status, provider_id=None):
        if not claim.new or status not in {"sent", "rejected", "unknown"}:
            return False
        try:
            return await asyncio.to_thread(self._complete, claim, status, provider_id)
        except (sqlite3.Error, OSError):
            return False


async def adc_token():
    """GCS uses service identity ADC, never the Gmail user's OAuth credential."""
    import google.auth
    from google.auth.transport.requests import Request

    class BoundedRequest(Request):
        def __call__(self, *args, **kwargs):
            kwargs["timeout"] = 4
            return super().__call__(*args, **kwargs)

    def refresh():
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
            quota_project_id="example-project",
        )
        credentials.refresh(BoundedRequest())
        return credentials.token

    async with asyncio.timeout(6):
        return await asyncio.to_thread(refresh)


class GcsLedger:
    def __init__(self, bucket, client=None, token_provider=None):
        if bucket != "example-project-colombia-a2a-mail-ledger":
            raise ValueError(
                "El registro compartido de Gmail exige el bucket dedicado de example-project"
            )
        self.bucket = bucket
        self.client = client
        self.token_provider = token_provider or adc_token

    def _object(self, key):
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise LedgerError("ledger_key_invalid")
        return f"claims/{key}.json"

    async def _request(self, method, url, **kwargs):
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=4, follow_redirects=False)
        try:
            token = await self.token_provider()
            return await client.request(
                method, url, headers={"Authorization": f"Bearer {token}"}, timeout=4, **kwargs
            )
        finally:
            if owned:
                await client.aclose()

    async def _upload(self, key, value, generation):
        return await self._request(
            "POST",
            f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o",
            params={
                "uploadType": "media",
                "name": self._object(key),
                "ifGenerationMatch": generation,
            },
            content=json.dumps(value).encode(),
        )

    async def reserve(self, key):
        response = await self._upload(
            key, {"status": "reserved", "provider_id": None, "time": time.time()}, "0"
        )
        if response.status_code == 412:
            existing = await self._request(
                "GET",
                f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/"
                + quote(self._object(key), safe=""),
                params={"alt": "media"},
            )
            existing.raise_for_status()
            data = existing.json()
            if data.get("status") not in {"reserved", "sent", "rejected", "unknown"}:
                raise LedgerError("ledger_record_invalid")
            return Claim(key, False, data["status"], data.get("provider_id"))
        response.raise_for_status()
        generation = response.json().get("generation")
        if not isinstance(generation, str) or not generation.isdigit():
            raise LedgerError("ledger_generation_unavailable")
        return Claim(key, True, generation=generation)

    async def complete(self, claim, status, provider_id=None):
        if (
            not claim.new
            or not claim.generation
            or not claim.generation.isdigit()
            or status not in {"sent", "rejected", "unknown"}
        ):
            return False
        try:
            response = await self._upload(
                claim.key,
                {"status": status, "provider_id": provider_id, "time": time.time()},
                claim.generation,
            )
            response.raise_for_status()
            return True
        except Exception:
            return False
