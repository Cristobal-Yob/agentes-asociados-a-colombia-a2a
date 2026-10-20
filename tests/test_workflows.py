from concurrent.futures import ThreadPoolExecutor


def store(path):
    from common.workflows import WorkflowStore

    return WorkflowStore(path, "email")


def test_sqlite_reopen_preserves_plan_approval_context_and_receipt(tmp_path):
    db = store(tmp_path / "email.sqlite")
    claim = db.begin("r", "s", {"message": "Borrador"}, now=100)
    assert claim["action"] == "execute"
    response = {
        "status": "preview",
        "message": "Revise",
        "context": "OPAQUE_CONTEXT",
        "preview": {"subject": "Repaso", "text": "Contenido", "expires_at": 400},
    }
    db.finish(claim, response, now=100)
    reopened = store(tmp_path / "email.sqlite")
    state = reopened.snapshot("r", "s")
    assert state["response"]["preview"]["text"] == "Contenido"
    assert state["approval"]["expires_at"] == 400
    assert state["operations"][0]["receipt"]["status"] == "preview"
    continuation = reopened.begin(
        "r", "s", {"message": "Sí, envíalo"}, expected_version=1, confirmation=True, now=101
    )
    assert continuation["context"] == "OPAQUE_CONTEXT" and continuation["version"] == 1


def test_cas_concurrency_and_crash_after_effect_never_reexecute(tmp_path):
    path = tmp_path / "email.sqlite"
    first, second = store(path), store(path)

    def begin(db):
        return db.begin("r", "s", {"message": "Envía"}, now=100)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(begin, [first, second]))
    assert sorted(claim["action"] for claim in claims) == ["execute", "unknown"]
    # El efecto externo pudo ocurrir antes de guardar recibo. Ni el reinicio ni
    # el vencimiento de una reserva autorizan repetirlo.
    retry = store(path).begin("r", "s", {"message": "Envía"}, now=100000)
    assert retry["action"] == "unknown"


def test_revisions_reject_stale_context_version_session_and_expired_approval(tmp_path):
    db = store(tmp_path / "email.sqlite")
    first = db.begin("r", "s", {"message": "Borrador"}, now=100)
    db.finish(
        first,
        {"status": "preview", "context": "C1", "preview": {"text": "A", "expires_at": 200}},
        now=100,
    )
    assert db.begin("r", "other", {"message": "Sí"}, now=101)["action"] == "session_mismatch"
    assert db.begin("r", "s", {"message": "Sí"}, context="OLD", now=101)["action"] == "stale"
    assert db.begin("r", "s", {"message": "Sí"}, confirmation=True, now=201)["action"] == "expired"
    revision = db.begin("r", "s", {"message": "Cambiar texto a B"}, expected_version=1, now=101)
    assert revision["version"] == 2
    db.finish(
        revision,
        {"status": "preview", "context": "C2", "preview": {"text": "B", "expires_at": 401}},
        now=101,
    )
    assert (
        db.begin("r", "s", {"message": "Sí"}, expected_version=1, confirmation=True, now=102)[
            "action"
        ]
        == "stale"
    )
    assert db.snapshot("r", "s")["version"] == 2


def test_replay_returns_original_receipt_without_new_operation(tmp_path):
    db = store(tmp_path / "email.sqlite")
    claim = db.begin("r", "s", {"message": "Envía"}, now=100)
    db.finish(
        claim, {"status": "sent", "delivery": "provider_accepted", "message": "Aceptado"}, now=100
    )
    replay = store(tmp_path / "email.sqlite").begin("r", "s", {"message": "Envía"}, now=101)
    assert replay["action"] == "replay" and replay["response"]["delivery"] == "provider_accepted"
    assert len(db.snapshot("r", "s")["operations"]) == 1


def test_semantic_digest_excludes_metadata_but_changed_approval_is_rejected(tmp_path):
    from common.workflows import semantic

    db = store(tmp_path / "email.sqlite")
    payload = {
        "action": "send",
        "preview": {"subject": "Repaso", "text": "Hola", "expires_at": 400, "draft_id": "A"},
        "approval_token": "TOKEN_A",
    }
    original = semantic(payload)
    missing = db.begin("new", "s", payload, confirmation=True, now=100)
    assert missing["action"] == "approval_mismatch"
    draft = db.begin("r", "s", {"message": "Borrador"}, now=100)
    db.finish(
        draft,
        {
            "status": "preview",
            "preview": payload["preview"],
            "approval_token": payload["approval_token"],
        },
        now=100,
    )
    claim = db.begin("r", "s", payload, confirmation=True, now=101)
    assert claim["action"] == "execute"
    db.finish(claim, {"status": "sent", "message": "Aceptado"}, now=101)
    payload["preview"]["expires_at"] = 500
    payload["preview"]["draft_id"] = "B"
    payload["approval_token"] = "TOKEN_B"
    assert semantic(payload) == original
    assert db.begin("r", "s", payload, confirmation=True, now=102)["action"] == "approval_mismatch"


def test_private_setup_is_idempotent_and_keys_are_separate(tmp_path):
    from scripts.setup_local import prepare

    first = prepare(tmp_path)
    paths = [
        tmp_path / "service-auth" / (role + "-api-key.txt")
        for role in ("tourism", "email", "agenda")
    ]
    keys = [path.read_bytes() for path in paths]
    assert len(set(keys)) == 3 and first["created"] == 4
    second = prepare(tmp_path)
    assert second["created"] == 0 and [path.read_bytes() for path in paths] == keys
    assert (tmp_path / "calendar-oauth-clases" / "approval-key.txt").is_file()
    assert not (tmp_path / "calendar-oauth-clases" / "authorized-user.json").exists()


def test_upstream_revision_with_new_operation_advances_peer_but_not_confirmation(tmp_path):
    db = store(tmp_path / "peer.sqlite")
    first = db.begin("r", "s", {"message": "Preparar"}, operation_id="phase-1")
    db.finish(first, {"status": "error", "error_code": "calendar_not_configured"})
    assert (
        db.begin(
            "r",
            "s",
            {"message": "Sí"},
            expected_version=3,
            operation_id="confirm",
            confirmation=True,
        )["action"]
        == "stale"
    )
    assert db.begin("r", "s", {"message": "Retoma"}, expected_version=3)["action"] == "stale"
    assert (
        db.begin(
            "r",
            "s",
            {"preview": {"plan_version": 1}, "approval_token": "old"},
            expected_version=3,
            operation_id="relabel",
        )["action"]
        == "approval_mismatch"
    )
    revision = db.begin("r", "s", {"message": "Retoma"}, expected_version=3, operation_id="phase-3")
    assert revision["action"] == "execute" and revision["version"] == 3
    db.finish(revision, {"status": "delivery_unknown"})
    assert (
        db.begin(
            "r", "s", {"message": "Otra revisión"}, expected_version=5, operation_id="phase-5"
        )["action"]
        == "unknown"
    )
