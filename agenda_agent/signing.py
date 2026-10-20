import base64
import hashlib
import hmac
import json
import secrets


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class AgendaSigner:
    def __init__(self, secret, fingerprint):
        if secret and not 32 <= len(secret.encode()) <= 4096:
            raise ValueError("La clave propia de Agenda debe tener entre 32 y 4096 bytes.")
        self.key = secret.encode() if secret else secrets.token_bytes(32)
        self.fingerprint = fingerprint

    def issue(self, session_id, state, now):
        claims = {
            "purpose": "agenda_plan",
            "session_id": session_id,
            "config": self.fingerprint,
            "issued_at": int(now),
            "expires_at": int(now) + 300,
            "state": state,
        }
        raw = canonical(claims)
        signature = hmac.new(self.key, b"agenda_plan\0" + raw, hashlib.sha256).hexdigest()
        token = base64.urlsafe_b64encode(raw).decode() + "." + signature
        if len(token) > 24000:
            raise ValueError("agenda_context_limit")
        return token

    def open(self, token, session_id, now):
        try:
            encoded, signature = token.split(".")
            raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
            expected = hmac.new(self.key, b"agenda_plan\0" + raw, hashlib.sha256).hexdigest()
            claims = json.loads(raw)
            if (
                not hmac.compare_digest(expected, signature)
                or claims["purpose"] != "agenda_plan"
                or claims["config"] != self.fingerprint
                or claims["session_id"] != session_id
                or not claims["issued_at"] <= now < claims["expires_at"]
            ):
                raise ValueError("approval_invalid")
            return claims["state"]
        except (ValueError, KeyError, TypeError):
            raise ValueError("approval_invalid") from None
