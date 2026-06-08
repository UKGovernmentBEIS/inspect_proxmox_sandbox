#!/usr/bin/env python3
"""Interact with OPNsense serial console via UNIX socket.

Handles login, menu navigation, and shell commands.

Deploy to the Proxmox host, then invoke:
    python3 /tmp/opn_serial.py 100 "ifconfig vtnet0" "pfctl -sr"

The root password defaults to the stock "opnsense". This provider randomizes
the root password per VM (see generate_config_xml in _impl/opnsense.py) and logs
the plaintext to the Inspect eval log, so pass it explicitly for those VMs:
    python3 /tmp/opn_serial.py --password <pw> 100 "pfctl -sr"
    OPNSENSE_ROOT_PASSWORD=<pw> python3 /tmp/opn_serial.py 100 "pfctl -sr"
"""

import argparse
import os
import socket
import time

STOCK_PASSWORD = "opnsense"


def serial_session(sock_path, commands, password=STOCK_PASSWORD, timeout=60):
    """Connect to serial socket, login, enter shell, run commands."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(sock_path)
    sock.setblocking(False)

    output = []

    def recv_all(wait=2):
        """Read all available data with timeout."""
        data = b""
        end = time.time() + wait
        while time.time() < end:
            try:
                chunk = sock.recv(4096)
                if chunk:
                    data += chunk
                    end = time.time() + 0.5  # extend on data
            except BlockingIOError:
                time.sleep(0.1)
        text = data.decode("utf-8", errors="replace")
        output.append(text)
        return text

    def send(text):
        sock.sendall((text + "\r\n").encode())

    def login():
        send("root")
        recv_all(2)
        send(password)
        return recv_all(8)  # wait for menu to appear

    # Flush any pending output
    recv_all(1)

    # Send Enter to trigger login or menu
    send("")
    resp = recv_all(3)

    # Determine state: login prompt, menu, or shell
    if "login:" in resp.lower():
        resp = login()
        output.append("[STATE: logged in]\n")
    elif "Enter an option:" in resp:
        output.append("[STATE: already at menu]\n")
    elif "#" in resp or "$" in resp:
        output.append("[STATE: already in shell]\n")
    else:
        # Try again - might need double enter
        send("")
        resp = recv_all(3)
        if "login:" in resp.lower():
            resp = login()

    # Enter shell (option 8)
    if "Enter an option:" in resp:
        send("8")
        resp = recv_all(5)
        output.append("[STATE: entered shell]\n")

    # Now run commands
    for cmd in commands:
        send(cmd)
        resp = recv_all(5)

    sock.close()
    return "".join(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vmid", help="Proxmox VM ID of the OPNsense VM")
    parser.add_argument("commands", nargs="*", help="Shell commands to run")
    parser.add_argument(
        "--password",
        default=os.environ.get("OPNSENSE_ROOT_PASSWORD", STOCK_PASSWORD),
        help='root password (default: $OPNSENSE_ROOT_PASSWORD or "opnsense")',
    )
    args = parser.parse_args()

    sock_path = f"/var/run/qemu-server/{args.vmid}.serial0"
    result = serial_session(sock_path, args.commands, password=args.password)
    print(result)
