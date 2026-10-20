import argparse
import asyncio
import json
import math
import statistics
import time
from pathlib import Path

from a2a.types import TaskState

from common.a2a_transport import send_typed
from scripts.processes import OwnedServices

TRIP = {"context": {"city": "Bogota", "days": 2, "interests": ["culture"]}}
DRAFT = {
    "recipient": "student@example.com",
    "request": "CLASSROOM_PRIVATE_BODY",
    "session_id": "smoke",
}


def distribution(samples):
    samples = sorted(samples)
    return {
        "n": len(samples),
        "p50_ms": round(statistics.median(samples), 2),
        "p95_ms": round(samples[max(0, math.ceil(len(samples) * 0.95) - 1)], 2),
    }


async def run_probe(samples=20):
    with OwnedServices() as services:

        async def send_peer(base, payload, **kwargs):
            role = "tourism" if base == services.tour_url else "email"
            return await send_typed(base, payload, api_key=services.keys[role], **kwargs)

        services.start("tourism")
        partial, _ = await send_peer(
            services.tour_url, {**TRIP, "draft_email": True, "recipient": "student@example.com"}
        )
        assert partial["status"] == "partial" and partial["itinerary"]
        services.start("email")
        for context, missing in [
            ({}, "city"),
            ({"city": "Bogota"}, "days"),
            ({"city": "Bogota", "days": 2}, "interests"),
            (TRIP["context"], None),
        ]:
            result, task = await send_peer(services.tour_url, {"context": context})
            assert result.get("missing_field") == missing
        assert task.status.state == TaskState.TASK_STATE_COMPLETED
        direct, direct_task = await send_peer(services.email_url, DRAFT)
        assert direct["status"] == "preview"
        assert direct_task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        assert direct["preview"]["transport"] == "mock", "La prueba nunca confirma correo real"
        send = {
            "action": "send",
            "session_id": "smoke",
            "preview": direct["preview"],
            "approval_token": direct["approval_token"],
            "confirmed": False,
        }
        rejected, _ = await send_peer(services.email_url, send)
        assert rejected["error_code"] == "approval_invalid"
        send["confirmed"] = True
        sent, _ = await send_peer(
            services.email_url, send, task_id=direct_task.id, context_id=direct_task.context_id
        )
        assert sent["status"] == "simulated" and sent["delivery"] == "simulated_only"
        integrated_payload = {**TRIP, "draft_email": True, "recipient": "student@example.com"}
        integrated, _ = await send_peer(services.tour_url, integrated_payload)
        assert integrated["status"] == "completed"
        assert integrated["email"]["status"] == "preview"
        measurements = {}
        for label, base, payload in [
            ("tourism_standalone", services.tour_url, TRIP),
            ("email_standalone", services.email_url, DRAFT),
            ("tourism_to_email", services.tour_url, integrated_payload),
        ]:
            elapsed = []
            for _ in range(samples):
                start = time.perf_counter()
                result, _ = await send_peer(base, payload)
                assert result["status"] in {"completed", "preview"}
                elapsed.append((time.perf_counter() - start) * 1000)
            measurements[label] = distribution(elapsed)

        async def concurrent_request():
            start = time.perf_counter()
            result, _ = await send_peer(services.tour_url, integrated_payload)
            assert result["status"] == "completed"
            return (time.perf_counter() - start) * 1000

        batch = await asyncio.gather(*[concurrent_request() for _ in range(8)])
        measurements["classroom_concurrency_8"] = distribution(batch)
        services.close()
        for path in services.logs:
            log = path.read_text(encoding="utf-8")
            for private in [
                "student@example.com",
                "CLASSROOM_PRIVATE_BODY",
                direct["approval_token"],
            ]:
                assert private not in log, "Los registros expusieron datos privados de la solicitud"
        assert all(process.poll() is not None for process, _ in services.children)
        return {
            "mode": "fixture determinista gratuito + mock",
            "network": "solo interfaz local",
            "protocol": "A2A 1.0 con SDK oficial 1.2.2 JSONRPC",
            "warm_samples_per_path": samples,
            "latency": measurements,
            "scenarios": [
                "turismo conserva el itinerario sin agente remoto",
                "aclaraciones del contrato tipado sin intérprete de texto",
                "vista previa directa de correo",
                "rechazo sin consentimiento explícito y confirmación simulada",
                "continuación nativa de tarea y contexto",
                "turismo delega mediante HTTP A2A",
                "8 solicitudes integradas simultáneas",
                "registros sin datos privados",
                "cierre de los procesos propios",
            ],
            "limits": "Prueba local con procesos ya iniciados; no mide carga de Cloud Run "
            "ni latencia del proveedor real.",
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100:
        parser.error("samples debe estar entre 1 y 100")
    report = asyncio.run(run_probe(args.samples))
    print(json.dumps(report, indent=2))
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
