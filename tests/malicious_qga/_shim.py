r"""Malicious QEMU guest-agent shim, run inside a sandbox guest.

Replaces the real `qemu-ga` on the virtio-serial port and answers the Proxmox
host with attacker-chosen JSON, so the runner-side test can exercise how the
provider treats guest-controlled response fields.

Runs on Python 3 stdlib only (guests have 3.12). It is deliberately dumb: it
serves one canned reply per command type, plus per-path replies for file-read
(keyed by the path from the preceding file-open). It executes nothing - the
provider's post-"exec" reads of stdout/stderr/returncode come back canned too.

Wire protocol (reverse-engineered from PVE::QMPClient, PVE 9.2): per API call
the host writes, on one connection,

    {"execute":"guest-sync-delimited","arguments":{"id":N}}\n
    {"execute":"<cmd>","arguments":{...}}\n

and parses the reply with a regex requiring a literal 0xFF, then the sync
return, then the command return, each newline-terminated:

    \\xff{"return":N}\n{"return":<result>}\n

So we emit 0xFF + the sync return when we see guest-sync-delimited, and the
bare command return for the following command.

A path ending in `shim.log` is served from the shim's own on-disk request log,
so the runner-side test can read back exactly what the shim saw and replied.
"""

import base64
import json
import os
import sys

PORT = "/dev/virtio-ports/org.qemu.guest_agent.0"
LOG = "/root/shim.log"

# QGA commands the provider issues that we must answer for a whole
# exec/read/write round-trip to complete against the shim.
_EMPTY_OK = frozenset(
    {
        "guest-file-close",
        "guest-fsfreeze-freeze",
        "guest-fsfreeze-thaw",
    }
)


def _log(msg):
    try:
        with open(LOG, "a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _load_scenario(path):
    with open(path) as f:
        return json.load(f)


def _handle(obj, state, scenario):
    """Return the raw QGA response object for one command, or None to skip."""
    cmd = obj.get("execute")
    args = obj.get("arguments") or {}
    responses = scenario.get("responses", {})

    if cmd == "guest-file-open":
        handle = state["next_handle"]
        state["next_handle"] += 1
        state["handles"][handle] = args.get("path", "")
        return {"return": handle}

    if cmd == "guest-file-read":
        path = state["handles"].get(args.get("handle"), "")
        if path.endswith("shim.log"):
            return _serve_log(args, state)
        return _file_read_reply(path, scenario)

    if cmd in responses:
        # Explicit per-command override (guest-exec, guest-exec-status, ...).
        return responses[cmd]

    if cmd == "guest-ping":
        return {"return": {}}

    if cmd == "guest-file-write":
        buf = args.get("buf-b64", "")
        try:
            n = len(base64.b64decode(buf))
        except Exception:
            n = 0
        return {"return": {"count": n, "eof": False}}

    if cmd == "guest-file-seek":
        return {"return": {"position": int(args.get("offset", 0)), "eof": False}}

    if cmd in _EMPTY_OK:
        return {"return": {}}

    return {"return": {}}


def _serve_log(args, state):
    try:
        with open(LOG, "rb") as f:
            data = f.read()
    except Exception:
        data = b""
    return {"return": {"buf-b64": base64.b64encode(data).decode(),
                       "count": len(data), "eof": True}}


def _file_read_reply(path, scenario):
    by_path = scenario.get("file_read_by_path", {})
    for suffix, reply in by_path.items():
        if suffix == "__default__":
            continue
        if path.endswith(suffix):
            return reply
    if "__default__" in by_path:
        return by_path["__default__"]
    return {"return": {"buf-b64": "", "count": 0, "eof": True}}


def _iter_lines(fd, buf):
    while True:
        nl = buf.find(b"\n")
        if nl == -1:
            chunk = os.read(fd, 65536)
            if not chunk:
                return
            buf += chunk
            continue
        line, buf = buf[:nl], buf[nl + 1 :]
        yield line.replace(b"\xff", b"").strip()


def _serve(scenario):
    state = {"handles": {}, "next_handle": 1000}
    _log("shim start pid=%d" % os.getpid())
    while True:
        try:
            fd = os.open(PORT, os.O_RDWR)
        except OSError as e:
            _log("open failed: %r" % e)
            continue
        buf = b""
        try:
            for line in _iter_lines(fd, buf):
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    _log("unparseable: %r" % line[:200])
                    continue
                cmd = obj.get("execute")
                if cmd == "guest-sync-delimited":
                    ident = (obj.get("arguments") or {}).get("id")
                    sync = json.dumps({"return": ident}).encode()
                    os.write(fd, b"\xff" + sync + b"\n")
                    _log("sync id=%r" % ident)
                    continue
                reply = _handle(obj, state, scenario)
                if reply is not None:
                    os.write(fd, json.dumps(reply).encode() + b"\n")
                    path = ""
                    if cmd == "guest-file-read":
                        handle = (obj.get("arguments") or {}).get("handle")
                        path = state["handles"].get(handle, "")
                    _log(
                        "cmd=%s path=%s reply=%s"
                        % (cmd, path, json.dumps(reply)[:200])
                    )
        except OSError as e:
            _log("serve OSError: %r" % e)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


if __name__ == "__main__":
    _serve(_load_scenario(sys.argv[1]))
