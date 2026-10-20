"""Reservas durables; una continuación incierta solo permite lectura posterior."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class AgendaLedger:
    def __init__(self, path):
        self.path = Path(path).resolve()
        if self.path.is_relative_to(Path(__file__).resolve().parents[1]):
            raise ValueError("El registro de Agenda debe estar fuera del proyecto.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS plans (key TEXT PRIMARY KEY, status "
                "TEXT NOT NULL, ids TEXT NOT NULL, intervals TEXT NOT NULL, "
                "mail_claimed INTEGER NOT NULL DEFAULT 0)"
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, key, ids, events):
        intervals = [(event.start.timestamp(), event.end.timestamp()) for event in events]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM plans WHERE key=?", (key,)).fetchone()
            if row:
                return False, row[0]
            for (encoded,) in db.execute(
                "SELECT intervals FROM plans WHERE status IN ('reserved','unknown')"
            ):
                if any(a < d and c < b for a, b in intervals for c, d in json.loads(encoded)):
                    return False, "conflict"
            db.execute(
                "INSERT INTO plans(key,status,ids,intervals) VALUES (?, 'reserved', ?, ?)",
                (key, json.dumps(ids), json.dumps(intervals)),
            )
            return True, "reserved"

    def complete(self, key, status):
        with self.connect() as db:
            db.execute("UPDATE plans SET status=? WHERE key=?", (status, key))

    def claim_mail(self, key):
        with self.connect() as db:
            return (
                db.execute(
                    (
                        "UPDATE plans SET mail_claimed=1 WHERE key=? AND mail_claimed=0 "
                        "AND status='created'"
                    ),
                    (key,),
                ).rowcount
                == 1
            )
