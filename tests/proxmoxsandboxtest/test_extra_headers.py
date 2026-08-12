"""Tests for per-instance extra HTTP headers."""

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import SecretStr

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox.schema import (
    HttpHeader,
    ProxmoxInstanceConfig,
    _load_instances_from_env_or_file,
)

HEADER_SENTINEL = "Bearer audit-token-sentinel-do-not-log"


def _instance_config(**overrides) -> ProxmoxInstanceConfig:
    values = dict(
        instance_id="proxy-instance",
        pool_id="proxy-pool",
        host="proxmox.example.com",
        port=443,
        user="root",
        user_realm="pam",
        password="password-sentinel",
        node="proxmox",
        verify_tls=True,
        extra_headers=(HttpHeader(name="Authorization", value=HEADER_SENTINEL),),
    )
    values.update(overrides)
    return ProxmoxInstanceConfig(**values)


def test_extra_headers_default_to_empty():
    config = _instance_config(extra_headers=())
    assert config.extra_headers == ()
    api = AsyncProxmoxAPI.from_instance_config(config)
    assert api.extra_headers == {}


def test_extra_headers_reach_the_api_client():
    api = AsyncProxmoxAPI.from_instance_config(_instance_config())
    assert api.extra_headers == {"Authorization": HEADER_SENTINEL}


def test_extra_header_values_are_redacted_in_config_representations():
    config = _instance_config()

    header = config.extra_headers[0]
    assert isinstance(header.value, SecretStr)
    assert header.value.get_secret_value() == HEADER_SENTINEL
    assert HEADER_SENTINEL not in repr(config)
    assert HEADER_SENTINEL not in str(config)
    assert HEADER_SENTINEL not in config.model_dump_json()


def test_instance_config_stays_hashable_with_extra_headers():
    hash(_instance_config())


def test_request_headers_include_extras_and_keep_proxmox_auth():
    api = AsyncProxmoxAPI.from_instance_config(_instance_config())
    api.ticket = "ticket-123"
    api.csrf_token = "csrf-456"

    headers = api._prepare_headers("POST", content_type=None)

    assert headers["Authorization"] == HEADER_SENTINEL
    assert headers["Cookie"] == "PVEAuthCookie=ticket-123"
    assert headers["CSRFPreventionToken"] == "csrf-456"


def test_curl_upload_headers_include_extras_and_keep_proxmox_auth():
    api = AsyncProxmoxAPI.from_instance_config(_instance_config())
    api.ticket = "ticket-123"
    api.csrf_token = "csrf-456"

    headers = api._curl_headers()

    assert f"Authorization: {HEADER_SENTINEL}" in headers
    assert "Cookie: PVEAuthCookie=ticket-123" in headers
    assert "CSRFPreventionToken: csrf-456" in headers


@pytest.mark.parametrize("name", ["Cookie", "cookie", "CSRFPreventionToken"])
def test_reserved_proxmox_auth_headers_are_rejected(name):
    with pytest.raises(ValueError, match="may not include"):
        AsyncProxmoxAPI(
            host="proxmox.example.com:443",
            user="root@pam",
            password="password",
            extra_headers={name: "shadowed"},
        )


@pytest.mark.asyncio
async def test_login_sends_extra_headers():
    api = AsyncProxmoxAPI.from_instance_config(_instance_config())
    api.request = AsyncMock(  # type: ignore[method-assign]
        return_value={"release": "9.2", "repoid": "abc", "version": "9.2.1"}
    )
    client = MagicMock()
    response = MagicMock()
    response.json.return_value = {
        "data": {"ticket": "ticket-123", "CSRFPreventionToken": "csrf-456"}
    }
    client.post = AsyncMock(return_value=response)

    await api._login(client)

    assert client.post.call_args.kwargs["headers"] == {"Authorization": HEADER_SENTINEL}


def test_extra_headers_participate_in_client_identity():
    def api(token: str) -> AsyncProxmoxAPI:
        return AsyncProxmoxAPI(
            host="proxmox.example.com:443",
            user="root@pam",
            password="password",
            extra_headers={"Authorization": token},
        )

    assert hash(api("Bearer one")) == hash(api("Bearer one"))
    assert hash(api("Bearer one")) != hash(api("Bearer two"))


def test_extra_headers_load_from_config_file(tmp_path, monkeypatch):
    config_path = tmp_path / "instances.json"
    config_path.write_text(
        json.dumps(
            {
                "instances": [
                    {
                        "instance_id": "test-1",
                        "pool_id": "ubuntu-pool",
                        "host": "proxmox.example.com",
                        "port": 443,
                        "user": "root",
                        "user_realm": "pam",
                        "password": "secret",
                        "node": "pve1",
                        "verify_tls": True,
                        "extra_headers": [
                            {"name": "Authorization", "value": HEADER_SENTINEL}
                        ],
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("PROXMOX_CONFIG_FILE", str(config_path))

    (instance,) = _load_instances_from_env_or_file()

    (header,) = instance.extra_headers
    assert header.name == "Authorization"
    assert header.value.get_secret_value() == HEADER_SENTINEL


def _mock_pve_transport(seen_auth_headers: dict[str, list[str]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen_auth_headers.setdefault(path, []).append(
            request.headers.get("Authorization", "")
        )
        if path.endswith("/access/ticket"):
            return httpx.Response(
                200,
                json={"data": {"ticket": "t", "CSRFPreventionToken": "c"}},
            )
        if path.endswith("/version"):
            return httpx.Response(
                200,
                json={"data": {"release": "9.2", "repoid": "abc", "version": "9.2.1"}},
            )
        if path.endswith("/agent/ping"):
            return httpx.Response(200, json={"data": None})
        if path.endswith("/agent/file-read"):
            return httpx.Response(
                200, json={"data": {"content": "aGVsbG8=", "truncated": False}}
            )
        return httpx.Response(404, json={"data": None})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_extra_headers_sent_on_file_read_path(monkeypatch):
    seen_auth_headers: dict[str, list[str]] = {}
    transport = _mock_pve_transport(seen_auth_headers)
    real_client = httpx.AsyncClient

    def client_with_mock_transport(**kwargs):
        kwargs.pop("verify", None)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_with_mock_transport)

    api = AsyncProxmoxAPI.from_instance_config(_instance_config())
    data, truncated = await api.read_file_capped("pve1", 100, "/tmp/file", count=1024)

    assert data == b"hello"
    assert truncated is False
    for path, headers in seen_auth_headers.items():
        assert headers == [HEADER_SENTINEL] * len(headers), path
    assert any(path.endswith("/agent/file-read") for path in seen_auth_headers)
