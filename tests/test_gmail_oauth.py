import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from email_agent.gmail_auth import SCOPES, SENDER, OAuthError
from scripts.authorize_gmail import OAuthFlow, load_client, save_credentials

CONFIG = {
    "project_id": "example-project",
    "client_id": "test.apps.googleusercontent.com",
    "client_secret": "SECRET_CLIENT",
}


def test_oauth_url_uses_loopback_state_nonce_pkce_and_narrow_scopes():
    flow = OAuthFlow(CONFIG, "http://127.0.0.1:12345/callback")
    query = parse_qs(urlsplit(flow.authorization_url()).query)
    assert query["login_hint"] == ["student@example.com"]
    assert set(query["scope"][0].split()) == SCOPES
    assert query["state"] == [flow.state] and len(flow.state) >= 32
    assert query["nonce"] == [flow.nonce] and len(flow.nonce) >= 32
    assert query["code_challenge_method"] == ["S256"]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(flow.verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert query["code_challenge"] == [challenge]
    assert "client_secret" not in query and query["redirect_uri"] == [flow.redirect_uri]


@pytest.mark.parametrize(
    "query,code",
    [
        ("state=wrong&code=SECRET_CODE", "state_mismatch"),
        ("state=stub&error=access_denied", "oauth_denied"),
    ],
)
def test_oauth_callback_rejects_wrong_state_and_denial_without_code_leak(query, code):
    flow = OAuthFlow(CONFIG, "http://127.0.0.1:12345/callback")
    query = query.replace("state=stub", "state=" + flow.state)
    with pytest.raises(OAuthError, match=code) as exc:
        flow.callback(query)
    assert "SECRET_CODE" not in str(exc.value)


def test_oauth_callback_is_one_shot_and_deadline_bounded():
    now = [1]
    flow = OAuthFlow(CONFIG, "http://127.0.0.1:12345/callback", clock=lambda: now[0])
    assert flow.callback("state=" + flow.state + "&code=SECRET_CODE") == "SECRET_CODE"
    with pytest.raises(OAuthError):
        flow.callback("state=" + flow.state + "&code=SECRET_CODE")
    expired = OAuthFlow(CONFIG, "http://127.0.0.1:12345/callback", clock=lambda: now[0])
    now[0] += 301
    with pytest.raises(OAuthError, match="flow_expired"):
        expired.callback("state=" + expired.state + "&code=SECRET_CODE")


@pytest.mark.parametrize(
    "field,value",
    [
        ("nonce", "wrong"),
        ("aud", "wrong"),
        ("iss", "wrong"),
        ("email", "wrong@example.com"),
        ("email_verified", False),
    ],
)
def test_oauth_verified_claims_must_match_flow_and_selected_account(field, value):
    flow = OAuthFlow(CONFIG, "http://127.0.0.1:12345/callback")
    claims = {
        "nonce": flow.nonce,
        "aud": CONFIG["client_id"],
        "iss": "https://accounts.google.com",
        "sub": "selected-sub",
        "email": SENDER,
        "email_verified": True,
    }
    claims[field] = value
    with pytest.raises(OAuthError):
        flow.accept_claims(claims, {"email": SENDER, "email_verified": True, "sub": "selected-sub"})


def test_secure_output_contains_only_refresh_credentials_and_never_overwrites(tmp_path, capsys):
    path = tmp_path / "authorized.json"
    values = {
        "type": "authorized_user",
        **CONFIG,
        "refresh_token": "SECRET_REFRESH",
        "account_email": SENDER,
        "account_sub": "selected-sub",
        "scopes": sorted(SCOPES),
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    save_credentials(path, values)
    assert json.loads(path.read_text()) == values
    assert not capsys.readouterr().out
    assert "access_token" not in path.read_text()
    with pytest.raises(OAuthError):
        save_credentials(path, values)


def test_code_exchange_uses_pinned_google_auth_verifier_and_pkce(monkeypatch):
    flow = OAuthFlow(CONFIG, "http://127.0.0.1:12345/callback")
    code = flow.callback("state=" + flow.state + "&code=SECRET_CODE")
    verified = []

    def verify(token, request, audience):
        verified.append((token, audience))
        assert request("https://www.googleapis.com/oauth2/v1/certs").status == 200
        return {
            "nonce": flow.nonce,
            "aud": audience,
            "iss": "https://accounts.google.com",
            "sub": "selected-sub",
            "email": SENDER,
            "email_verified": True,
        }

    monkeypatch.setattr("scripts.authorize_gmail.id_token.verify_oauth2_token", verify)

    def respond(request):
        if request.url.path == "/token":
            body = parse_qs(request.content.decode())
            assert body["code_verifier"] == [flow.verifier]
            assert body["code"] == [code]
            return httpx.Response(
                200,
                json={
                    "scope": " ".join(SCOPES),
                    "token_type": "Bearer",
                    "access_token": "SECRET_ACCESS",
                    "id_token": "TEST_ID_TOKEN",
                    "refresh_token": "SECRET_REFRESH",
                },
            )
        if request.url.path.endswith("certs"):
            return httpx.Response(200, json={})
        return httpx.Response(
            200, json={"email": SENDER, "email_verified": True, "sub": "selected-sub"}
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        values = flow.exchange(code, client)
        assert values["account_sub"] == "selected-sub" and "access_token" not in values
        assert verified == [("TEST_ID_TOKEN", CONFIG["client_id"])]
        with pytest.raises(OAuthError):
            flow.exchange(code, client)


@pytest.mark.parametrize(
    "values", [{"web": CONFIG}, {"installed": {**CONFIG, "project_id": "wrong"}}]
)
def test_only_verified_project_desktop_client_configuration_is_accepted(tmp_path, values):
    path = tmp_path / "client.json"
    path.write_text(json.dumps(values))
    with pytest.raises(OAuthError):
        load_client(path)


def test_downloaded_google_desktop_auth_uri_does_not_override_fixed_endpoints(tmp_path):
    path = tmp_path / "desktop.json"
    path.write_text(
        json.dumps(
            {
                "installed": {
                    **CONFIG,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        )
    )
    config = load_client(path)
    flow = OAuthFlow(config, "http://127.0.0.1:12345/callback")
    assert flow.authorization_url().startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    config["auth_uri"] = "https://attacker.invalid/auth"
    path.write_text(json.dumps({"installed": config}))
    with pytest.raises(OAuthError):
        load_client(path)
