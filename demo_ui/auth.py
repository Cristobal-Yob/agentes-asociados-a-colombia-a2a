"""Google Web OAuth only. This client never requests Gmail or refresh access."""

import asyncio

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import id_token


class BoundedGoogleRequest:
    def __call__(self, **kwargs):
        kwargs["timeout"] = 8
        return Request()(**kwargs)


class GoogleOIDC:
    def __init__(self, config):
        self.config = config

    async def exchange(self, code, verifier, nonce):
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            async with client.stream(
                "POST",
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "redirect_uri": self.config.origin + "/auth/callback",
                    "grant_type": "authorization_code",
                    "code_verifier": verifier,
                },
            ) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 64_000:
                        raise ValueError("Token response exceeds limit")
        import json

        token = json.loads(data).get("id_token")
        if not isinstance(token, str) or len(token) > 16_000:
            raise ValueError("Missing identity token")
        claims = await asyncio.to_thread(
            id_token.verify_oauth2_token, token, BoundedGoogleRequest(), self.config.client_id
        )
        if claims.get("azp", self.config.client_id) != self.config.client_id:
            raise ValueError("Unexpected authorized party")
        return claims
