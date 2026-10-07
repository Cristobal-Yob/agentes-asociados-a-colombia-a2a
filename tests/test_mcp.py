import json

import pytest
from mcp import types

from tourism_agent.mcp_catalog import McpCatalog, records


class Session:
    def __init__(self):
        self.calls = []
        self.lists = 0

    async def list_tools(self):
        self.lists += 1
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="get_items_by_name",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "resource": {"type": "string"},
                            "name": {"type": "string"},
                        },
                        "required": ["resource", "name"],
                    },
                ),
                types.Tool(
                    name="get_touristic_attractions_by_department",
                    input_schema={
                        "type": "object",
                        "properties": {"departmentId": {"type": "integer"}},
                        "required": ["departmentId"],
                    },
                ),
                types.Tool(name="send_email", input_schema={}),
            ]
        )

    async def call_tool(self, name, args, **kwargs):
        self.calls.append((name, args))
        data = (
            [{"id": 1, "name": "Bogota", "departmentId": 2}]
            if name == "get_items_by_name"
            else [
                {"id": 10, "name": "Example from untrusted tool text", "cityId": 1},
                {"id": 11, "name": "Different city", "cityId": 9},
            ]
        )
        return types.CallToolResult(
            is_error=False,
            structured_content=None,
            content=[types.TextContent(type="text", text=json.dumps(data))],
        )


@pytest.mark.asyncio
async def test_mcp_allowlist_schema_cache_and_city_grounding():
    catalog = McpCatalog()
    session = Session()
    sources, status = await catalog.fetch(session, "Bogota")
    assert status == "mcp_live"
    assert len(sources) == 1
    assert sources[0].retrieved_at
    assert len(session.calls) == 2
    assert session.calls[0][1] == {"resource": "city", "name": "Bogota"}
    await catalog.fetch(session, "Bogota")
    assert session.lists == 1
    assert {name for name, _ in session.calls} <= {
        "get_items_by_name",
        "get_touristic_attractions_by_department",
    }


@pytest.mark.asyncio
async def test_mcp_unavailable_retains_explicit_fallback_and_cache():
    calls = 0

    def unavailable(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise ConnectionError

    catalog = McpCatalog(connector=unavailable)
    result = await catalog.retrieve("Bogota")
    assert result == ([], "mcp_unavailable_fixture_fallback")
    assert await catalog.retrieve("Bogota") == result
    assert calls == 1


def test_native_sdk_structured_result_and_error_flag_are_respected():
    # The wire uses camelCase, but pinned SDK 2.3 Python models use snake_case.
    result = types.CallToolResult.model_validate(
        {"content": [], "structuredContent": {"data": [{"id": 1}]}, "isError": False}
    )
    assert records(result) == [{"id": 1}]
    rejected = types.CallToolResult.model_validate(
        {"content": [], "structuredContent": {"data": [{"id": 1}]}, "isError": True}
    )
    with pytest.raises(ValueError, match="mcp_tool_error"):
        records(rejected)
