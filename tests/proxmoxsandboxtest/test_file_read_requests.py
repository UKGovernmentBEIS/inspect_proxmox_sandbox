"""A guest file read is one agent request, plus login when there is no ticket."""

import httpx
import pytest

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI


class _Peer:
    def __init__(self):
        self.requests: list[str] = []
        self.rejected_ticket: str | None = None
        self.tickets_issued = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path.rsplit("/", 1)[-1])
        if path.endswith("/access/ticket"):
            self.tickets_issued += 1
            ticket = f"t{self.tickets_issued}"
            return httpx.Response(
                200, json={"data": {"ticket": ticket, "CSRFPreventionToken": "c"}}
            )
        if path.endswith("/version"):
            return httpx.Response(
                200, json={"data": {"release": "9.2", "repoid": "x", "version": "9.2"}}
            )
        if path.endswith("/agent/file-read"):
            cookie = request.headers["Cookie"]
            if cookie == f"PVEAuthCookie={self.rejected_ticket}":
                return httpx.Response(401, json={"data": None})
            return httpx.Response(
                200, json={"data": {"content": "aGk=", "truncated": False}}
            )
        return httpx.Response(404, json={"data": None})


@pytest.fixture
def peer(monkeypatch) -> _Peer:
    peer = _Peer()
    real_client = httpx.AsyncClient

    def client_with_mock_transport(**kwargs):
        kwargs.pop("verify", None)
        return real_client(transport=httpx.MockTransport(peer.handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_with_mock_transport)
    return peer


def _api() -> AsyncProxmoxAPI:
    return AsyncProxmoxAPI("proxmox.test:8006", "user@pam", "password")


async def test_first_file_read_logs_in_then_reads(peer: _Peer):
    api = _api()
    assert await api.read_file_capped("n", 1, "/f", 16) == (b"hi", False)
    assert peer.requests == [
        "ticket",
        "version",
        "file-read",
    ]


async def test_later_file_reads_are_a_single_request(peer: _Peer):
    api = _api()
    await api.read_file_capped("n", 1, "/f", 16)
    peer.requests.clear()
    assert await api.read_file_capped("n", 1, "/f", 16) == (b"hi", False)
    assert peer.requests == ["file-read"]


async def test_file_read_logs_in_again_when_the_ticket_is_rejected(peer: _Peer):
    api = _api()
    await api.read_file_capped("n", 1, "/f", 16)
    peer.rejected_ticket = api.ticket
    peer.requests.clear()
    assert await api.read_file_capped("n", 1, "/f", 16) == (b"hi", False)
    assert peer.requests == [
        "file-read",
        "ticket",
        "version",
        "file-read",
    ]
