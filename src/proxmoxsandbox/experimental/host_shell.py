"""Run a script as root on a Proxmox node using only the HTTP API.

Use sparingly. This drives the web UI's Shell (termproxy) and scrapes the pty,
which is the only way to run arbitrary commands through pveproxy. It needs a
root@pam password login, and the framing below is pve-xtermjs's wire format, not
the documented REST API. Host configuration belongs in the provisioning scripts;
this is for checks and one-off diagnostics, not for fixing hosts at runtime.

`POST /nodes/{node}/termproxy` starts a login shell on the node and returns a
one-shot ticket; `/nodes/{node}/vncwebsocket` then relays the pty over a
websocket. After the newline-terminated ``user:ticket`` handshake the server
answers ``OK``, the client frames input as ``0:<len>:<bytes>`` and resizes as
``1:<cols>:<rows>:``, and pty output comes back unframed.
"""

import asyncio
import base64
import shlex
import ssl
import uuid
from typing import Sequence
from urllib.parse import quote

import websockets

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI


async def run_script_on_host(
    api: AsyncProxmoxAPI,
    node: str,
    script: str,
    args: Sequence[str] = (),
    timeout: float = 120,
) -> tuple[int, str]:
    """Run `script` with bash on the node; returns (exit code, combined pty output).

    Output is whatever the script wrote to the terminal, so stdout and stderr are
    interleaved and CRLF-translated. Needs a root@pam login (termproxy would
    prompt other users for a password).
    """
    term = await api.request("POST", f"/nodes/{node}/termproxy")
    url = (
        f"{api.api_base_url.replace('https://', 'wss://', 1)}"
        f"/nodes/{node}/vncwebsocket"
        f"?port={term['port']}&vncticket={quote(term['ticket'], safe='')}"
    )
    ssl_context = ssl.create_default_context()
    if not api.verify_tls:
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    # The typed command is echoed back before `stty -echo` takes effect, so the
    # markers are spelt with a quote break in the command and whole in the output.
    token = uuid.uuid4().hex
    marker = f"{token}-"
    typed_marker = f"{token[:8]}''{token[8:]}-"
    payload = base64.b64encode(script.encode()).decode()
    command = (
        f"stty -echo; echo {typed_marker}BEGIN; "
        f"base64 -d <<'EOF' | bash -s -- {shlex.join(args)}; "
        f"echo {typed_marker}END $?\n"
        f"{payload}\nEOF\nexit\n"
    )

    async with websockets.connect(
        url,
        ssl=ssl_context,
        additional_headers={"Cookie": f"PVEAuthCookie={api.ticket}"},
        subprotocols=[websockets.Subprotocol("binary")],
        max_size=None,
    ) as ws:
        await ws.send(f"{term['user']}:{term['ticket']}\n".encode())
        ok = await asyncio.wait_for(ws.recv(), timeout)
        if ok != b"OK":
            raise RuntimeError(f"termproxy handshake failed: {ok!r}")
        await ws.send(b"1:200:50:")
        data = command.encode()
        await ws.send(f"0:{len(data)}:".encode() + data)

        end_marker = f"{marker}END ".encode()

        async def read_until_end() -> bytes:
            buffer = b""
            while True:
                chunk = await ws.recv()
                buffer += chunk if isinstance(chunk, bytes) else chunk.encode()
                if end_marker in buffer and b"\n" in buffer.split(end_marker, 1)[1]:
                    return buffer

        buffer = await asyncio.wait_for(read_until_end(), timeout)

    text = buffer.decode(errors="replace").replace("\r\n", "\n")
    body = text.split(f"{marker}BEGIN\n", 1)[1]
    output, tail = body.split(f"{marker}END ", 1)
    return int(tail.split()[0]), output
