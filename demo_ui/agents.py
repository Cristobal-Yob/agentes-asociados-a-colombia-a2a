"""Fixed upstream destinations, bounded responses, no automatic effect retries."""

import json

import httpx


class AgentClient:
    def __init__(self, config):
        self.targets = {
            "itinerary": (config.itinerary_url, config.itinerary_key),
            "email": (config.email_url, config.email_key),
        }

    async def request(self, agent, method, path, **kwargs):
        base, key = self.targets[agent]
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(95, connect=8), follow_redirects=False
        ) as client:
            async with client.stream(
                method, base + path, headers={"X-API-Key": key}, **kwargs
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 500_000:
                        raise ValueError("Agent response exceeds limit")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError("Invalid agent response")
        return result

    async def post(self, agent, payload):
        path = "/api/chat" if agent == "itinerary" else "/api/email"
        return await self.request(agent, "POST", path, json=payload)

    async def lookup(self, agent, request_id, session_id):
        from urllib.parse import quote

        return await self.request(
            agent,
            "GET",
            "/api/requests/" + quote(request_id, safe=""),
            params={"session_id": session_id},
        )
