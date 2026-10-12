"""Guest HTTP replies are bounded before parsing or decompression."""

import base64
import json
import time
from functools import partial

import httpx
import pytest

from proxmoxsandbox._impl import async_proxmox
from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI, ProxmoxVersionInfo
from proxmoxsandbox._impl.qga_responses import GuestAgentTamperError


class ObservedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def make_api(monkeypatch, handler, *, release="9.2", follow_redirects=False):
    client = partial(
        httpx.AsyncClient,
        transport=httpx.MockTransport(handler),
        follow_redirects=follow_redirects,
    )
    monkeypatch.setattr(httpx, "AsyncClient", client)
    api = AsyncProxmoxAPI(
        "proxmox.test:8006",
        "user",
        "password",
        extra_headers={"accept-encoding": "gzip", "X-Proxy-Token": "proxy-token"},
    )
    api.ticket = "old-ticket"
    api.ticket_date = time.monotonic()
    api.csrf_token = "old-csrf"
    api.discovered_proxmox_version = ProxmoxVersionInfo(
        release=release, repoid="test", version=release
    )
    return api


@pytest.mark.parametrize(
    "operation, data",
    [
        ("ping", {}),
        ("exec", {"pid": 42}),
        ("exec-status", {"exited": 1, "out-data": "result"}),
        ("file-write", {}),
    ],
)
async def test_guest_requests_keep_authentication_and_parse_streamed_json(
    monkeypatch, operation, data
):
    body = json.dumps({"data": data}).encode()
    stream = ObservedStream([body[:8], body[8:]])

    def handler(request):
        assert request.url.host == "proxmox.test"
        assert request.headers.get_list("Accept-Encoding") == ["identity"]
        assert request.headers["Cookie"] == "PVEAuthCookie=old-ticket"
        assert request.headers["CSRFPreventionToken"] == "old-csrf"
        assert request.headers["X-Proxy-Token"] == "proxy-token"
        return httpx.Response(200, stream=stream)

    api = make_api(monkeypatch, handler)
    result = await api.request("POST", f"/nodes/pve/qemu/100/agent/{operation}")
    assert result == data
    assert stream.reads == 2
    assert stream.closed


@pytest.mark.parametrize("operation", ["request", "read_file"])
async def test_oversized_guest_body_closes_before_parsing_or_consuming_the_rest(
    monkeypatch, operation
):
    monkeypatch.setattr(async_proxmox, "_GUEST_RESPONSE_ENVELOPE_BYTES", 32)
    limit = 32 if operation == "request" else 32 + 8 * 2
    stream = ObservedStream([b"x" * limit, b"!", b"not consumed"])
    parsed = []
    original_json = httpx.Response.json

    def parse(response, **kwargs):
        if response.request.url.path.endswith("/ping"):
            return original_json(response, **kwargs)
        parsed.append(response)
        raise AssertionError("oversized guest response reached JSON parsing")

    monkeypatch.setattr(httpx.Response, "json", parse)

    def handler(request):
        if request.url.path.endswith("/ping"):
            return httpx.Response(200, json={"data": None})
        return httpx.Response(200, stream=stream)

    api = make_api(monkeypatch, handler)
    with pytest.raises(GuestAgentTamperError, match="byte allowance") as raised:
        if operation == "request":
            await api.request("POST", "/nodes/pve/qemu/100/agent/exec")
        else:
            await api.read_file_capped("pve", 100, "/sample", 2)
    assert raised.value.vm_id == 100
    assert stream.reads == 2
    assert stream.closed
    assert not parsed


@pytest.mark.parametrize("operation", ["request", "read_file"])
@pytest.mark.parametrize("status", [200, 500])
@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "gzip, identity"])
async def test_encoded_guest_body_is_rejected_before_reading_or_decompression(
    monkeypatch, operation, status, encoding
):
    stream = ObservedStream([b"must not be read"])

    def handler(request):
        if request.url.path.endswith("/ping"):
            return httpx.Response(200, json={"data": None})
        return httpx.Response(
            status, headers={"Content-Encoding": encoding}, stream=stream
        )

    api = make_api(monkeypatch, handler)
    with pytest.raises(GuestAgentTamperError, match="content encoding"):
        if operation == "request":
            await api.request("POST", "/nodes/pve/qemu/100/agent/exec")
        else:
            await api.read_file_capped("pve", 100, "/sample", 2)
    assert stream.reads == 0
    assert stream.closed


@pytest.mark.parametrize("operation", ["request", "read_file"])
async def test_error_body_keeps_a_safe_prefix_and_closes_without_reading_the_rest(
    monkeypatch, operation
):
    monkeypatch.setattr(async_proxmox, "_MAX_ERROR_RESPONSE_BYTES", 32)
    stream = ObservedStream(
        [b"failed to open file: ", b"\x1b[31m" + b"x" * 64, b"not consumed"]
    )

    def handler(request):
        if request.url.path.endswith("/ping"):
            return httpx.Response(200, json={"data": None})
        return httpx.Response(
            500,
            headers={"Content-Length": "1000000"},
            extensions={"reason_phrase": b"Agent error \x1b[31m"},
            stream=stream,
        )

    api = make_api(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError) as raised:
        if operation == "request":
            await api.request("POST", "/nodes/pve/qemu/100/agent/exec")
        else:
            await api.read_file_capped("pve", 100, "/sample", 2)
    error = raised.value
    assert "failed to open file" in str(error)
    assert "HTTP response body truncated" in str(error)
    assert "\x1b" not in str(error)
    assert r"\x1b" in str(error)
    assert error.response.status_code == 500
    assert len(error.response.content) == 32
    assert error.response.headers["Content-Length"] == "32"
    assert error.request.url.host == "proxmox.test"
    assert stream.reads == 2
    assert stream.closed


async def test_partial_error_body_is_not_parsed_when_raising_was_disabled(monkeypatch):
    monkeypatch.setattr(async_proxmox, "_MAX_ERROR_RESPONSE_BYTES", 8)
    stream = ObservedStream([b'{"error": "too long"}', b"not consumed"])
    api = make_api(monkeypatch, lambda _: httpx.Response(500, stream=stream))
    with pytest.raises(httpx.HTTPStatusError, match="body truncated"):
        await api.request("POST", "/nodes/pve/qemu/100/agent/exec", raise_errors=False)
    assert stream.closed
    assert stream.reads == 1


@pytest.mark.parametrize("second_status", [200, 401])
async def test_guest_401_refreshes_authentication_and_retries_only_once(
    monkeypatch, second_status
):
    monkeypatch.setattr(async_proxmox, "_MAX_ERROR_RESPONSE_BYTES", 8)
    first = ObservedStream([b"old ticket expired", b"not consumed"])
    requests = []
    launches = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/access/ticket"):
            return httpx.Response(
                200,
                json={
                    "data": {"ticket": "new-ticket", "CSRFPreventionToken": "new-csrf"}
                },
            )
        if request.url.path.endswith("/version"):
            return httpx.Response(
                200,
                json={"data": {"release": "9.2", "repoid": "test", "version": "9.2"}},
            )
        assert request.url.path.endswith("/agent/exec")
        assert request.headers.get_list("Accept-Encoding") == ["identity"]
        launches.append(request)
        if len(launches) == 1:
            return httpx.Response(401, stream=first)
        return httpx.Response(second_status, json={"data": {"pid": 42}})

    api = make_api(monkeypatch, handler)
    if second_status == 200:
        assert await api.request(
            "POST", "/nodes/pve/qemu/100/agent/exec", json={"command": ["true"]}
        ) == {"pid": 42}
    else:
        with pytest.raises(httpx.HTTPStatusError) as raised:
            await api.request("POST", "/nodes/pve/qemu/100/agent/exec")
        assert raised.value.response.status_code == 401
    assert len(requests) == 4
    assert len(launches) == 2
    assert launches[0].content == launches[1].content
    assert launches[0].headers["Cookie"] == "PVEAuthCookie=old-ticket"
    assert launches[1].headers["Cookie"] == "PVEAuthCookie=new-ticket"
    assert launches[1].headers["CSRFPreventionToken"] == "new-csrf"
    assert {request.url.host for request in requests} == {"proxmox.test"}
    assert first.reads == 1
    assert first.closed


@pytest.mark.parametrize("operation", ["request", "read_file"])
async def test_guest_location_cannot_change_the_authenticated_request_destination(
    monkeypatch, operation
):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/ping"):
            return httpx.Response(200, json={"data": None})
        data = {"pid": 42} if operation == "request" else {"content": "YQ=="}
        return httpx.Response(
            302,
            headers={"Location": "https://other.invalid/collect"},
            json={"data": data},
        )

    api = make_api(monkeypatch, handler, follow_redirects=True)
    if operation == "request":
        assert await api.request("POST", "/nodes/pve/qemu/100/agent/exec") == {
            "pid": 42
        }
    else:
        assert await api.read_file_capped("pve", 100, "/sample", 2) == (b"a", False)
    assert len(requests) == (1 if operation == "request" else 2)
    assert {request.url.host for request in requests} == {"proxmox.test"}


async def test_modern_file_budget_allows_padded_chunks_and_escaped_solidus(monkeypatch):
    monkeypatch.setattr(async_proxmox, "_GUEST_RESPONSE_ENVELOPE_BYTES", 32)
    payload = b"\xff\xff\xff"
    content = "".join(base64.b64encode(bytes([byte])).decode() for byte in payload)
    body = json.dumps({"data": {"content": content}}, separators=(",", ":"))
    encoded = body.replace("/", r"\/").encode()
    assert len(encoded) <= async_proxmox._guest_response_limit("file-read", count=4)
    stream = ObservedStream([encoded])

    def handler(request):
        if request.url.path.endswith("/ping"):
            return httpx.Response(200, json={"data": None})
        assert request.headers.get_list("Accept-Encoding") == ["identity"]
        assert request.url.params["file"] == "/sample?literal&filename"
        assert request.url.params["count"] == "4"
        assert request.url.params["decode"] == "0"
        return httpx.Response(200, stream=stream)

    api = make_api(monkeypatch, handler)
    assert await api.read_file_capped("pve", 100, "/sample?literal&filename", 4) == (
        payload,
        False,
    )
    assert stream.closed


async def test_legacy_file_budget_covers_the_full_read_before_client_truncation(
    monkeypatch,
):
    monkeypatch.setattr(async_proxmox, "_GUEST_RESPONSE_ENVELOPE_BYTES", 32)
    monkeypatch.setattr(async_proxmox, "_MAX_GUEST_FILE_BYTES", 4)
    body = json.dumps({"data": {"content": "\x00" * 4}}).encode()
    assert len(body) <= async_proxmox._guest_response_limit("file-read")

    def handler(request):
        if request.url.path.endswith("/ping"):
            return httpx.Response(200, json={"data": None})
        assert dict(request.url.params) == {"file": "/sample"}
        return httpx.Response(200, stream=ObservedStream([body]))

    api = make_api(monkeypatch, handler, release="9.1")
    assert await api.read_file_capped("pve", 100, "/sample", 1) == (b"\x00", True)


async def test_status_budget_allows_two_json_escaped_byte_streams(monkeypatch):
    monkeypatch.setattr(async_proxmox, "_GUEST_RESPONSE_ENVELOPE_BYTES", 64)
    monkeypatch.setattr(async_proxmox, "MAX_EXEC_DATA_CHARS", 4)
    data = {"exited": 1, "out-data": "\x00" * 4, "err-data": "\x00" * 4}
    body = json.dumps({"data": data}).encode()
    assert len(body) <= async_proxmox._guest_response_limit("exec-status")
    api = make_api(
        monkeypatch, lambda _: httpx.Response(200, stream=ObservedStream([body]))
    )
    assert (
        await api.request("GET", "/nodes/pve/qemu/100/agent/exec-status?pid=42") == data
    )


@pytest.mark.parametrize(
    "control", ["\n", "\r", "\t", "\x1b", "\x7f", "\x85", "\u202e", "\u200b"]
)
def test_error_text_escapes_terminal_and_unicode_format_controls(control):
    rendered = async_proxmox._bounded_error_text(f"No such file {control}suffix")
    assert rendered.startswith("No such file ")
    assert control not in rendered
    assert control.encode("unicode_escape").decode() in rendered


def test_error_text_is_still_bounded_after_escaping(monkeypatch):
    monkeypatch.setattr(async_proxmox, "_MAX_ERROR_TEXT_CHARS", 32)
    rendered = async_proxmox._bounded_error_text("\x1b" * 100)
    assert len(rendered) <= 32 + len("... (truncated)")
    assert "\x1b" not in rendered


async def test_diagnostic_escaping_does_not_change_guest_file_bytes(monkeypatch):
    payload = b"\x1b[31mdata\r\n" + "\u202e".encode()

    def handler(request):
        data = (
            None
            if request.url.path.endswith("/ping")
            else {"content": base64.b64encode(payload).decode()}
        )
        return httpx.Response(200, json={"data": data})

    api = make_api(monkeypatch, handler)
    assert await api.read_file_capped("pve", 100, "/sample", len(payload) + 1) == (
        payload,
        False,
    )
