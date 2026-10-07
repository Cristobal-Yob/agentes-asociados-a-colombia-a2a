import base64
import hashlib
import hmac
import json
import secrets


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class ApprovalSigner:
    def __init__(self, key):
        self.key = key.encode() if key else secrets.token_bytes(32)

    def issue(self, preview, config_fingerprint):
        claims = {
            "digest": hashlib.sha256(canonical(preview.model_dump(mode="json"))).hexdigest(),
            "draft_id": preview.draft_id,
            "session_id": preview.session_id,
            "expires_at": preview.expires_at,
            "config": config_fingerprint,
        }
        raw = canonical(claims)
        return (
            base64.urlsafe_b64encode(raw).decode()
            + "."
            + hmac.new(self.key, raw, hashlib.sha256).hexdigest()
        )

    def verify(self, token, preview, session_id, config_fingerprint, now):
        try:
            encoded, signature = token.split(".")
            raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
            if not hmac.compare_digest(
                signature, hmac.new(self.key, raw, hashlib.sha256).hexdigest()
            ):
                return False
            claims = json.loads(raw)
            expected = hashlib.sha256(canonical(preview.model_dump(mode="json"))).hexdigest()
            return (
                claims["digest"] == expected
                and claims["draft_id"] == preview.draft_id
                and claims["session_id"] == session_id == preview.session_id
                and claims["config"] == config_fingerprint
                and now < claims["expires_at"] == preview.expires_at
                and now >= preview.expires_at - 300
            )
        except (ValueError, KeyError, TypeError):
            return False

    def issue_context(self, state, config_fingerprint, now, expires_at=None):
        claims = {
            "purpose": "email_conversation",
            "state": state,
            "config": config_fingerprint,
            "issued_at": int(now),
            "expires_at": expires_at if expires_at is not None else int(now) + 300,
        }
        raw = canonical(claims)
        token = (
            base64.urlsafe_b64encode(raw).decode()
            + "."
            + hmac.new(self.key, b"email_conversation\0" + raw, hashlib.sha256).hexdigest()
        )
        if len(token) > 24000:
            raise ValueError("conversation_context_limit")
        return token

    def open_context(self, token, config_fingerprint, session_id, now):
        try:
            if len(token) > 24000:
                raise ValueError("conversation_context_limit")
            encoded, signature = token.split(".")
            raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
            expected = hmac.new(self.key, b"email_conversation\0" + raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("conversation_context_invalid")
            claims = json.loads(raw)
            state = claims["state"]
            if (
                claims["purpose"] != "email_conversation"
                or claims["config"] != config_fingerprint
                or not claims["issued_at"] <= now < claims["expires_at"]
                or session_id is not None
                and state["session_id"] != session_id
            ):
                raise ValueError("conversation_context_invalid")
            return state
        except (ValueError, KeyError, TypeError):
            raise ValueError("conversation_context_invalid") from None
