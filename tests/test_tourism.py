import pytest

from common.config import Settings
from common.contracts import TourismRequest, TripContext
from tourism_agent.service import TourismService


@pytest.mark.asyncio
async def test_greeting_never_retrieves_and_asks_one_question():
    service = TourismService(Settings.offline())
    result = await service.handle(TourismRequest(message="Hola"))
    assert result.status == "clarification"
    assert result.missing_field == "city"
    assert service.catalog.calls == 0


@pytest.mark.asyncio
async def test_grounded_itinerary_and_unsupported_city():
    service = TourismService(Settings.offline())
    context = TripContext(city="Bogota", days=2, interests=["culture"])
    result = await service.handle(TourismRequest(context=context))
    assert result.status == "completed"
    assert len(result.itinerary.days) == 2
    assert result.itinerary.sources
    assert "no están verificados" in " ".join(result.itinerary.warnings).lower()
    bad = await service.handle(
        TourismRequest(context=TripContext(city="Unknown", days=2, interests=["culture"]))
    )
    assert bad.status == "error"
