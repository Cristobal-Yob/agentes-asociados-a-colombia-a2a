import json
from pathlib import Path

from common.config import Settings
from common.contracts import (
    EmailChatRequest,
    EmailRequest,
    EmailResponse,
    TourismRequest,
    TourismResponse,
)
from email_agent.app import create_app as email_app
from tourism_agent.app import create_app as tour_app


def main():
    destination = Path(__file__).resolve().parents[1] / "docs" / "contracts"
    destination.mkdir(exist_ok=True)
    for contract in [
        EmailChatRequest,
        EmailRequest,
        EmailResponse,
        TourismRequest,
        TourismResponse,
    ]:
        (destination / f"{contract.__name__}.schema.json").write_text(
            json.dumps(contract.model_json_schema(), indent=2) + "\n", encoding="utf-8"
        )
    for name, factory, port in [("tourism", tour_app, 8001), ("email", email_app, 8002)]:
        app = factory(Settings.offline(public_url=f"http://127.0.0.1:{port}"))
        (destination / f"{name}.openapi.json").write_text(
            json.dumps(app.openapi(), indent=2) + "\n", encoding="utf-8"
        )
        # Served native card is inspected during protocol/process tests.
        assert app.state.service
    print("Se exportaron cinco esquemas JSON tipados y dos contratos REST OpenAPI.")


if __name__ == "__main__":
    main()
