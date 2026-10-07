import asyncio
import json
import time
from datetime import UTC, datetime

from jsonschema import validate
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from common.contracts import Source
from tourism_agent.catalog import normalize

MCP_URL = "https://api-colombia.com/api/v1/mcp"
ALLOWED = {"get_items_by_name", "get_touristic_attractions_by_department"}


def records(result):
    if getattr(result, "is_error", False):
        raise ValueError("mcp_tool_error")
    data = getattr(result, "structured_content", None)
    if data is None:
        texts = [item.text for item in result.content if getattr(item, "type", "") == "text"]
        text = "\n".join(texts)
        if len(text) > 40000:
            raise ValueError("mcp_result_limit")
        data = json.loads(text)
    for _ in range(3):
        if isinstance(data, dict):
            data = data.get("data", data.get("items", data.get("results", [])))
    return data[:30] if isinstance(data, list) else []


def tool_arguments(schema, values):
    args = {}
    for key in schema.get("properties", {}):
        if key in values:
            args[key] = values[key]
    validate(args, schema)  # Unknown required schema fields fail closed.
    return args


class McpCatalog:
    """Public read-only MCP. Two allowed tool calls; no sampling or actions."""

    def __init__(self, connector=streamable_http_client, clock=time.monotonic):
        self.connector = connector
        self.clock = clock
        self.schemas = {}
        self.schema_until = 0
        self.cache = {}
        self.lock = asyncio.Lock()

    async def retrieve(self, city):
        key = normalize(city)
        cached = self.cache.get(key)
        if cached and cached[0] > self.clock():
            return cached[1], cached[2]
        async with self.lock:
            cached = self.cache.get(key)
            if cached and cached[0] > self.clock():
                return cached[1], cached[2]
            try:
                async with asyncio.timeout(10):
                    async with self.connector(MCP_URL, max_sse_event_size=65536) as streams:
                        async with ClientSession(*streams, read_timeout_seconds=4) as session:
                            await session.initialize()
                            sources, status = await self.fetch(session, city)
            except TimeoutError:
                sources, status = [], "mcp_timeout_fixture_fallback"
            except Exception:
                sources, status = [], "mcp_unavailable_fixture_fallback"
            self.cache[key] = (self.clock() + (900 if sources else 60), sources, status)
            return sources, status

    async def fetch(self, session, city):
        if self.schema_until <= self.clock():
            listing = await session.list_tools()
            self.schemas = {
                tool.name: tool.input_schema for tool in listing.tools if tool.name in ALLOWED
            }
            self.schema_until = self.clock() + 600
        if set(self.schemas) != ALLOWED:
            return [], "mcp_schema_unavailable_fixture_fallback"
        args = tool_arguments(
            self.schemas["get_items_by_name"],
            {
                "resource": "city",
                "name": city,
                "itemName": city,
                "type": "City",
                "itemType": "City",
                "entity": "City",
            },
        )
        cities = records(await session.call_tool("get_items_by_name", args, read_timeout_seconds=4))
        matches = [
            item
            for item in cities
            if isinstance(item, dict) and normalize(str(item.get("name", ""))) == normalize(city)
        ]
        if not matches:
            return [], "mcp_empty_fixture_fallback"
        found = matches[0]
        if not isinstance(found.get("id"), int) or isinstance(found["id"], bool):
            return [], "mcp_city_identity_unavailable_fixture_fallback"
        department = found.get("departmentId", found.get("idDepartment"))
        if not isinstance(department, int) or isinstance(department, bool):
            return [], "mcp_department_unavailable_fixture_fallback"
        args = tool_arguments(
            self.schemas["get_touristic_attractions_by_department"],
            {"departmentId": department, "idDepartment": department, "id": department},
        )
        attractions = records(
            await session.call_tool(
                "get_touristic_attractions_by_department", args, read_timeout_seconds=4
            )
        )
        # Department membership alone does not prove the activity is in the requested city.
        valid = [
            item
            for item in attractions
            if isinstance(item, dict)
            and item.get("cityId") == found.get("id")
            and isinstance(item.get("name"), str)
            and isinstance(item.get("id"), int)
        ]
        now = datetime.now(UTC).isoformat()
        sources = [
            Source(
                id=f"mcp-{item['id']}",
                name=item["name"][:150],
                city=city,
                url=f"https://api-colombia.com/api/v1/TouristicAttraction/{item['id']}",
                retrieved_at=now,
                provenance="live_mcp",
            )
            for item in valid[:3]
        ]
        return sources, "mcp_live" if sources else "mcp_empty_fixture_fallback"
