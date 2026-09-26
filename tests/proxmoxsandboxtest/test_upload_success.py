"""Happy-path upload over a real local HTTP connection."""

import asyncio
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI


async def _serve_ok(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    headers = await reader.readuntil(b"\r\n\r\n")
    length = next(
        int(line.split(b":", 1)[1])
        for line in headers.split(b"\r\n")
        if line.lower().startswith(b"content-length:")
    )
    if b"expect: 100-continue" in headers.lower():
        writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        await writer.drain()
    body = await reader.readexactly(length)
    assert b"sample upload" in body
    payload = json.dumps({"data": {"volid": "local:iso/payload.iso"}}).encode()
    writer.write(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload
    )
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _upload() -> dict:
    async with await asyncio.start_server(_serve_ok, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        api = AsyncProxmoxAPI("unused.test", "user", "password")
        api.api_base_url = f"http://127.0.0.1:{port}"
        api.ticket, api.csrf_token, api.ticket_date = "t", "c", time.monotonic()
        with TemporaryDirectory() as directory:
            payload = Path(directory) / "payload.iso"
            payload.write_bytes(b"sample upload")
            return await api.upload_file("node", "local", payload, "iso")


def test_successful_upload_returns_parsed_data():
    assert asyncio.run(_upload()) == {"volid": "local:iso/payload.iso"}
