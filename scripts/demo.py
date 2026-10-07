import argparse
import asyncio
import json

from common.a2a_transport import send_typed


async def demo(args):
    payload = {
        "message": "Bogotá, 2 días, cultura",
        "draft_email": True,
        "recipient": args.recipient,
        "session_id": "interactive-demo",
    }
    result, _ = await send_typed(args.tourism_url, payload)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    draft = result.get("email")
    if not draft or draft.get("status") != "preview":
        return
    preview = draft["preview"]
    print(
        "\nRevise el destinatario, asunto, texto, remitente y transporte exactos mostrados arriba."
    )
    if args.mock_confirm:
        if preview["transport"] != "mock":
            raise RuntimeError("La confirmación automática solo se permite para MOCK")
        confirm = True
    else:
        confirm = (
            input(
                "Escriba SEND para confirmar explícitamente esta vista previa exacta, "
                "o Enter para detenerse: "
            )
            == "SEND"
        )
    if confirm:
        result, _ = await send_typed(
            args.email_url,
            {
                "action": "send",
                "session_id": payload["session_id"],
                "confirmed": True,
                "preview": preview,
                "approval_token": draft["approval_token"],
            },
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(
        description="Muestra una vista previa y solicita su confirmación explícita."
    )
    parser.add_argument(
        "--recipient", required=True, help="Destinatario explícito que debe revisar"
    )
    parser.add_argument("--tourism-url", default="http://127.0.0.1:8001")
    parser.add_argument("--email-url", default="http://127.0.0.1:8002")
    parser.add_argument("--mock-confirm", action="store_true")
    asyncio.run(demo(parser.parse_args()))


if __name__ == "__main__":
    main()
