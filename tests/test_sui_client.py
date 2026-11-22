"""Focused contract tests for the additive S-UI adapter."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx

from app.services.sui import SuiClient


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        sui_base_url="http://127.0.0.1:2095/app",
        sui_api_token="test-token",
        sui_subscription_base_url="http://203.0.113.10:2096/sub",
        sui_inbound_ids="",
        sui_admin_username="s-ui",
    )


def _run(awaitable):
    return asyncio.run(awaitable)


def test_direct_links_decode_subscription_and_filter_unrelated_lines():
    link = "hysteria2://password@example.com:443#test"
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sub/alice"
        return httpx.Response(200, text=base64.b64encode((link + "\nhttps://example.com").encode()).decode())
    client = SuiClient(settings=_settings(), http_client=httpx.AsyncClient(
        base_url="http://127.0.0.1:2095/app/apiv2",
        transport=httpx.MockTransport(handler),
    ))
    try:
        assert _run(client.direct_links("alice")) == [link]
    finally:
        _run(client.close())


def test_sui_reads_client_as_existing_user_contract() -> None:
    raw = {
        "id": 7,
        "enable": True,
        "name": "alice",
        "inbounds": [1],
        "volume": 10_000,
        "expiry": 4_000_000_000,
        "up": 100,
        "down": 200,
        "desc": "demo",
        "remark": "demo",
        "links": [],
    }
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        assert request.headers["Token"] == "test-token"
        return httpx.Response(
            200,
            json={"success": True, "msg": "", "obj": {"clients": [raw]}},
        )

    client = SuiClient(
        settings=_settings(),
        http_client=httpx.AsyncClient(
            base_url="http://127.0.0.1:2095/app/apiv2",
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        user = _run(client.get_user_by_username("alice"))
    finally:
        _run(client.close())

    assert seen_paths == ["/app/apiv2/clients"]
    assert user["username"] == "alice"
    assert user["used_traffic"] == 300
    assert user["data_limit"] == 10_000
    assert user["subscription_url"] == "http://203.0.113.10:2096/sub/alice"


def test_sui_create_translates_quota_expiry_and_inbounds() -> None:
    saved: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/inbounds"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "obj": [{"id": 1, "type": "vless", "tag": "main", "enable": True}],
                },
            )
        if request.url.path.endswith("/save"):
            fields = parse_qs(request.content.decode())
            saved.update(json.loads(fields["data"][0]))
            return httpx.Response(200, json={"success": True, "obj": {}})
        return httpx.Response(
            200,
            json={
                "success": True,
                "obj": {
                    "clients": [
                        {
                            "id": 8,
                            "enable": True,
                            "name": "bob",
                            "inbounds": [1],
                            "volume": 2_000,
                            "expiry": 4_000_000_000,
                            "up": 0,
                            "down": 0,
                            "links": [],
                        }
                    ]
                },
            },
        )

    client = SuiClient(
        settings=_settings(),
        http_client=httpx.AsyncClient(
            base_url="http://127.0.0.1:2095/app/apiv2",
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        user = _run(
            client.create_user(
                {
                    "username": "bob",
                    "group_ids": [1],
                    "data_limit": 2_000,
                    "expire": "2096-10-02T00:00:00Z",
                    "status": "active",
                }
            )
        )
    finally:
        _run(client.close())

    assert saved["name"] == "bob"
    assert saved["inbounds"] == [1]
    assert saved["volume"] == 2_000
    assert saved["expiry"] > 0
    assert user["subscription_token"] == "bob"


def test_edit_disable_enable_and_delete_target_the_requested_client() -> None:
    raw = {"id": 8, "name": "bob", "enable": True, "volume": 1000, "expiry": 0,
           "config": {"vless": {"uuid": "local-test"}}, "inbounds": [1], "up": 10, "down": 20}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.params["id"] == "8"
            return httpx.Response(200, json={"success": True, "obj": {"clients": [
                {"id": 7, "name": "alice"}, raw,
            ]}})
        fields = parse_qs(request.content.decode())
        data = json.loads(fields["data"][0])
        assert fields["object"] == ["clients"]
        assert data["id"] == 8
        if fields["action"] == ["del"]:
            assert data == {"id": 8}
            raw.clear()
        else:
            assert fields["action"] == ["edit"]
            assert data["config"] == {"vless": {"uuid": "local-test"}}
            raw.update(data)
        return httpx.Response(200, json={"success": True, "obj": None})

    async def exercise() -> None:
        client = SuiClient(
            settings=_settings(),
            http_client=httpx.AsyncClient(
                base_url="http://127.0.0.1:2095/app/apiv2",
                transport=httpx.MockTransport(handler),
            ),
        )
        try:
            edited = await client.modify_user_by_id(8, {"data_limit": 2000, "expire": 4_000_000_000})
            assert edited["data_limit"] == 2000
            assert edited["expiry"] == 4_000_000_000
            assert (await client.get_user_usage(8))["used_traffic"] == 30
            assert (await client.set_disabled_by_id(8, True))["status"] == "disabled"
            assert (await client.set_disabled_by_id(8, False))["status"] == "active"
            await client.delete_user_by_id(8)
            assert not raw
        finally:
            await client.close()

    _run(exercise())


def test_stats_include_summary_for_existing_dashboard() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        obj = {"sys": {"appVersion": "1.6.7"}} if request.url.path.endswith("/status") else {
            "clients": [{"enable": True}, {"enable": False}],
        }
        return httpx.Response(200, json={"success": True, "obj": obj})

    async def exercise() -> None:
        client = SuiClient(
            settings=_settings(),
            http_client=httpx.AsyncClient(
                base_url="http://127.0.0.1:2095/app/apiv2",
                transport=httpx.MockTransport(handler),
            ),
        )
        try:
            summary = await client.get_system_stats()
            assert summary["version"] == "1.6.7"
            assert summary["total_users"] == 2
            assert summary["users_active"] == summary["users_disabled"] == 1
        finally:
            await client.close()

    _run(exercise())
