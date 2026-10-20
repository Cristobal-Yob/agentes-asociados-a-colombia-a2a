from common.config import Settings
from common.contracts import EmailRequest
from email_agent.service import EmailService


async def test_typed_calendar_summary_requires_normal_signed_preview_before_send():
    summary = "Repaso: 2030-05-06T10:00:00-05:00\nhttps://calendar.google.com/event?eid=test"
    service = EmailService(Settings.offline(approval_secret="e" * 40))
    draft = await service.handle(
        EmailRequest(
            action="draft",
            recipient="student@example.com",
            content={"subject": "Agenda confirmada", "text": summary},
        )
    )
    assert draft.status == "preview"
    assert draft.preview.text == summary and draft.preview.subject == "Agenda confirmada"
    refused = await service.handle(
        EmailRequest(
            action="send",
            preview=draft.preview,
            approval_token=draft.approval_token,
            confirmed=False,
        )
    )
    assert refused.error_code == "approval_invalid"
    sent = await service.handle(
        EmailRequest(
            action="send",
            preview=draft.preview,
            approval_token=draft.approval_token,
            confirmed=True,
        )
    )
    assert sent.status == "simulated"
