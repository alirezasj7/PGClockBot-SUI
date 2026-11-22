"""Verify a first-run S-UI form reaches the API and persists only on success."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import app as web
from app.services import pasarguard, pg_access, sui


@pytest.mark.parametrize("reply", ["success", "invalid_token", "html"])
def test_setup_form_without_legacy_fields(monkeypatch, reply):
    saved = []
    original_client = sui.SuiClient

    def handle(request):
        assert request.url.path == "/app/apiv2/status"
        assert request.url.params["r"] == "sys"
        assert request.headers["Token"] == "local-test-token"
        if reply == "html":
            return httpx.Response(200, text="<html>Login</html>")
        if reply == "invalid_token":
            return httpx.Response(200, json={"success": False, "msg": "invalid token"})
        return httpx.Response(200, json={"success": True, "obj": {"sys": {}}})

    def make_client(*, settings):
        return original_client(
            settings=settings,
            http_client=httpx.AsyncClient(
                base_url=f"{settings.sui_base_url}/apiv2",
                transport=httpx.MockTransport(handle),
            ),
        )

    monkeypatch.setattr(sui, "SuiClient", make_client)
    monkeypatch.setattr(web, "is_setup_complete", lambda: False)
    monkeypatch.setattr(web, "is_local_setup_client", lambda ip: True)
    monkeypatch.setattr(web, "begin_setup", lambda: None)
    monkeypatch.setattr(web, "ensure_setup_gate_token", lambda: "")
    monkeypatch.setattr(web, "ensure_web_secret", lambda: None)
    monkeypatch.setattr(web, "current_setup_values", lambda: {"SUI_API_TOKEN": "local-test-token"})
    monkeypatch.setattr(web, "update_env_keys", saved.append)
    with TestClient(web.create_api_app(), base_url="http://127.0.0.1") as client:
        response = client.post(
            "/setup/other",
            data={
                "sui_base_url": "http://127.0.0.1:2095/app",
                "sui_subscription_base_url": "http://203.0.113.10:2096/sub",
                "sui_inbound_ids": "1,2,1",
            },
            headers={"X-Requested-With": "setup-probe", "Origin": "http://127.0.0.1"},
        )
    assert "local-test-token" not in response.text
    if reply == "success":
        assert response.status_code == 200, response.text
        assert response.json()["next"] == "/setup?step=4"
        assert saved[0]["SUI_INBOUND_IDS"] == "1,2"
        assert saved[0]["SUI_API_TOKEN"] == "local-test-token"
    else:
        assert response.status_code == 400, response.text
        assert response.json()["ok"] is False
        assert not saved


def test_owner_menu_uses_sui_and_other_shops_cannot_reuse_owner_client(monkeypatch):
    monkeypatch.setattr(pasarguard, "get_pg", lambda: SimpleNamespace(
        is_auth_ready=True, get_current_admin=owner_info,
    ))

    async def exercise():
        staff = await pg_access.enrich_platform_admin_staff({"role": "admin"})
        assert set(staff["pg_permissions"]) == {"pg_overview", "pg_users", "pg_inbounds"}
        caps = await pg_access.resolve_platform_pg_capabilities()
        bot_staff = pg_access.staff_from_platform_caps(caps)
        assert set(bot_staff["pg_permissions"]) == set(staff["pg_permissions"])
        with pytest.raises(pasarguard.PasarGuardError) as denied:
            await pasarguard.get_pg_for_reseller(None, 1)
        assert denied.value.status_code == 403

    async def owner_info():
        return {"username": "s-ui"}

    asyncio.run(exercise())
