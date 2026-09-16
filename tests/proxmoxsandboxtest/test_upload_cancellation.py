"""Exercise upload cancellation against a real local HTTP connection."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI


async def upload_then_cancel() -> None:
    received = asyncio.Event()
    disconnected = asyncio.Event()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
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
        received.set()
        assert await reader.read() == b""
        disconnected.set()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        api = AsyncProxmoxAPI("unused.test", "user", "password")
        api.api_base_url = f"http://127.0.0.1:{port}"
        with TemporaryDirectory() as directory:
            payload = Path(directory) / "payload.iso"
            payload.write_bytes(b"sample upload")
            upload = asyncio.create_task(
                api.upload_file_with_curl("node", "local", payload, "iso")
            )
            await asyncio.wait_for(received.wait(), 3)
            upload.cancel()
            try:
                await asyncio.wait_for(upload, 2)
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("upload cancellation was swallowed")
            await asyncio.wait_for(disconnected.wait(), 2)


def test_cancelling_an_upload_closes_the_connection_without_freezing_python():
    worker = subprocess.run(
        [sys.executable, "-m", "tests.proxmoxsandboxtest.test_upload_cancellation"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert worker.returncode == 0, worker.stderr
    assert json.loads(worker.stdout) == {"cancelled": True, "disconnected": True}


if __name__ == "__main__":
    asyncio.run(upload_then_cancel())
    print(json.dumps({"cancelled": True, "disconnected": True}))
