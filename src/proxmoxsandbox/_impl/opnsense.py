"""OPNsense gateway VM support for domain-based egress filtering.

This module handles:
- config.xml generation from SubnetConfig(type="opnsense") parameters
- Unbound DNS whitelist generation
- Static base template creation (one-time Docker + UFS2Tool to add rc.d script)
- Per-eval config ISO generation (pycdlib) and attachment

The base OPNsense image is built once with an rc.d script that reads
config from a CDROM at boot. Per-eval config is delivered as a tiny ISO
containing config.xml and dns_whitelist.conf, attached as ide2.

Docker is only needed for the one-time base image build (adding the rc.d
script to the UFS2 filesystem). It is NOT needed at eval time.
"""

import asyncio
import hashlib
import secrets
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from importlib.resources import files
from io import BytesIO
from logging import getLogger
from pathlib import Path
from typing import BinaryIO, Sequence, cast

import pycdlib
import tenacity
from inspect_ai.util import trace_action

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.qemu_commands import QemuCommands
from proxmoxsandbox._impl.storage_commands import LOCAL_STORAGE, LocalStorageCommands
from proxmoxsandbox._impl.task_wrapper import TaskWrapper
from proxmoxsandbox.schema import SubnetConfig

OPNSENSE_NANO_URL = (
    "https://mirror.ams1.nl.leaseweb.net/opnsense/releases/25.1/"
    "OPNsense-25.1-nano-amd64.img.bz2"
)

OPNSENSE_BASE_TEMPLATE_TAG_PREFIX = "opnsense-base-"

DOCKER_IMAGE_NAME = "opnsense-injector"

# Local cache dir for the stock OPNsense image
STOCK_IMAGE_CACHE_DIR = Path.home() / ".cache" / "opnsense-injector"
STOCK_QCOW2_NAME = "OPNsense-25.1-nano-amd64.qcow2"
BASE_QCOW2_FILENAME = "opnsense-base.qcow2"

VM_TIMEOUT = 1200

TRACE_NAME = "proxmox_opnsense"

# FreeBSD rc.d script that copies config from CDROM at boot.
# Runs BEFORE configd so OPNsense loads the CDROM-provided config.xml.
# OPNsense syshook early script that copies config from CDROM at boot.
# Placed in /usr/local/etc/rc.syshook.d/early/ — runs before configd,
# before networking, before OPNsense reads config.xml.
OPNSENSE_CONFIG_IMPORT_SCRIPT = """\
#!/bin/sh
# Import config.xml and dns_whitelist.conf from CDROM if present.
# This runs as an OPNsense syshook early script — before configd starts.

TAG="opnsense_config_import"

# Find CDROM device
_dev=""
for d in /dev/cd0 /dev/cd1 /dev/acd0; do
    if [ -e "$d" ]; then
        _dev="$d"
        break
    fi
done

if [ -z "$_dev" ]; then
    logger -t "$TAG" "No CDROM device found, skipping"
    exit 0
fi

logger -t "$TAG" "Found CDROM at $_dev"

mount_cd9660 "$_dev" /mnt 2>/dev/null
if [ $? -ne 0 ]; then
    logger -t "$TAG" "Failed to mount $_dev"
    exit 0
fi

if [ -f /mnt/config.xml ]; then
    cp /mnt/config.xml /conf/config.xml
    logger -t "$TAG" "Imported config.xml from $_dev"
fi

if [ -f /mnt/dns_whitelist.conf ]; then
    mkdir -p /usr/local/etc/unbound.opnsense.d
    cp /mnt/dns_whitelist.conf /usr/local/etc/unbound.opnsense.d/dns_whitelist.conf
    logger -t "$TAG" "Imported dns_whitelist.conf from $_dev"
fi

umount /mnt 2>/dev/null
exit 0
"""

# Path where the script is placed inside the OPNsense image.
# OPNsense rc.syshook.d/early/ scripts run at boot before configd.
OPNSENSE_CONFIG_IMPORT_PATH = (
    "/usr/local/etc/rc.syshook.d/early/09-config-import"
)


def generate_unbound_whitelist_conf(subnet: SubnetConfig) -> str:
    """Generate an Unbound config that refuses resolution for non-whitelisted domains.

    This is placed in /usr/local/etc/unbound.opnsense.d/ on the OPNsense image.
    It blocks DNS resolution (returns REFUSED) for all domains except those in
    the whitelist, preventing information leakage via DNS.
    """
    assert subnet.domain_whitelist is not None
    lines = ["server:"]
    # Default policy: refuse all queries
    lines.append('    local-zone: "." refuse')
    # Allow resolution for each whitelisted domain
    for domain in subnet.domain_whitelist:
        # Strip trailing dot if present, then add it (Unbound requires trailing dot)
        domain = domain.rstrip(".")
        lines.append(f'    local-zone: "{domain}." transparent')
    return "\n".join(lines) + "\n"


def generate_config_xml(
    subnet: SubnetConfig,
    static_maps: Sequence[tuple[str, str, str | None]] = (),
) -> str:
    """Generate an OPNsense config.xml from a SubnetConfig with type="opnsense".

    Args:
        subnet: The OPNsense-managed subnet configuration.
        static_maps: Sequence of (mac, ipv4, hostname) tuples for DHCP
            static mappings on the LAN. Derived from VmNicConfig.mac/ipv4
            on VMs connected to the OPNsense LAN VNet.
    """
    assert subnet.type == "opnsense"
    assert subnet.domain_whitelist is not None

    reference_path = (
        files("proxmoxsandbox") / "scripts" / "experimental" / "opnsense_config.xml"
    )
    tree = ET.parse(str(reference_path))
    root = tree.getroot()

    lan_ip = str(subnet.gateway)
    lan_prefix = subnet.cidr.prefixlen

    # Update LAN interface IP
    lan_if = root.find(".//interfaces/lan")
    if lan_if is not None:
        ipaddr = lan_if.find("ipaddr")
        if ipaddr is not None:
            ipaddr.text = lan_ip
        subnet_el = lan_if.find("subnet")
        if subnet_el is not None:
            subnet_el.text = str(lan_prefix)

    # Update DHCP settings
    dhcpd_lan = root.find(".//dhcpd/lan")
    if dhcpd_lan is not None:
        dhcp_range = dhcpd_lan.find("range")
        if dhcp_range is not None and subnet.dhcp_ranges:
            first_range = subnet.dhcp_ranges[0]
            from_el = dhcp_range.find("from")
            if from_el is not None:
                from_el.text = str(first_range.start)
            to_el = dhcp_range.find("to")
            if to_el is not None:
                to_el.text = str(first_range.end)
        gateway = dhcpd_lan.find("gateway")
        if gateway is not None:
            gateway.text = lan_ip
        dnsserver = dhcpd_lan.find("dnsserver")
        if dnsserver is not None:
            dnsserver.text = lan_ip

        # Add DHCP static mappings
        for mac, ipv4, hostname in static_maps:
            staticmap = ET.SubElement(dhcpd_lan, "staticmap")
            ET.SubElement(staticmap, "mac").text = mac.upper()
            ET.SubElement(staticmap, "ipaddr").text = ipv4
            if hostname:
                ET.SubElement(staticmap, "hostname").text = hostname

    # Update domain whitelist alias
    alias = root.find(".//OPNsense/Firewall/Alias/aliases/alias")
    if alias is not None:
        content = alias.find("content")
        if content is not None:
            content.text = "\n".join(subnet.domain_whitelist)

    # Randomize root password — defence-in-depth. Generate a random
    # plaintext password, hash it with bcrypt, and log the plaintext
    # so operators can access the gateway for debugging.
    import bcrypt as _bcrypt

    plaintext_password = secrets.token_urlsafe(16)
    hashed = _bcrypt.hashpw(
        plaintext_password.encode(), _bcrypt.gensalt(rounds=10)
    ).decode()
    # OPNsense expects $2y$ prefix (PHP-compatible); bcrypt lib uses $2b$
    hashed = hashed.replace("$2b$", "$2y$", 1)

    logger = getLogger(__name__)
    logger.info(f"OPNsense root password: {plaintext_password}")

    user_el = root.find(".//system/user")
    if user_el is not None:
        pw_el = user_el.find("password")
        if pw_el is not None:
            pw_el.text = hashed

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def base_template_tag() -> str:
    """Deterministic tag for the OPNsense base template.

    Includes the stock image URL and the rc.d script content, so changes
    to either invalidate the cached template. Config-independent.
    """
    h = hashlib.sha256(
        (OPNSENSE_NANO_URL + OPNSENSE_CONFIG_IMPORT_SCRIPT).encode()
    ).hexdigest()[:8]
    return f"{OPNSENSE_BASE_TEMPLATE_TAG_PREFIX}{h}"


def _ensure_docker_image() -> None:
    """Build the opnsense-injector Docker image if not already present."""
    logger = getLogger(__name__)

    # Check if image exists
    result = subprocess.run(
        ["docker", "image", "inspect", DOCKER_IMAGE_NAME],
        capture_output=True,
    )
    if result.returncode == 0:
        logger.info("Docker injector image already exists")
        return

    logger.info("Building Docker injector image...")
    dockerfile_dir = (
        files("proxmoxsandbox") / "scripts" / "experimental" / "opnsense_injector"
    )
    subprocess.run(
        [
            "docker",
            "build",
            "-t",
            DOCKER_IMAGE_NAME,
            "-f",
            str(Path(str(dockerfile_dir)) / "Dockerfile"),
            str(dockerfile_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    logger.info("Docker injector image built successfully")


def _ensure_stock_image_local() -> Path:
    """Download and prepare the stock OPNsense qcow2 if not cached locally."""
    logger = getLogger(__name__)
    STOCK_IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stock_path = STOCK_IMAGE_CACHE_DIR / STOCK_QCOW2_NAME

    if stock_path.exists():
        logger.info(f"Stock OPNsense image cached at {stock_path}")
        return stock_path

    logger.info(f"Downloading OPNsense nano image from {OPNSENSE_NANO_URL}...")
    with tempfile.TemporaryDirectory() as tmpdir:
        bz2_path = Path(tmpdir) / "opnsense.img.bz2"
        raw_path = Path(tmpdir) / "opnsense.img"

        subprocess.run(
            ["wget", "-q", OPNSENSE_NANO_URL, "-O", str(bz2_path)],
            check=True,
        )
        logger.info("Decompressing...")
        subprocess.run(["bunzip2", str(bz2_path)], check=True)

        logger.info("Converting raw -> qcow2...")
        subprocess.run(
            [
                "qemu-img",
                "convert",
                "-f",
                "raw",
                "-O",
                "qcow2",
                str(raw_path),
                str(stock_path),
            ],
            check=True,
        )

    logger.info(f"Stock image cached at {stock_path}")
    return stock_path


def _docker_inject_rcscript(stock_qcow2: Path) -> Path:
    """Add the syshook config-import script to the base image.

    Returns the path to the base qcow2 (in a temp dir that the caller
    should clean up after uploading).
    """
    logger = getLogger(__name__)
    work_dir = Path(tempfile.mkdtemp(prefix="opnsense-base-"))

    # Write the syshook script to the work dir
    rcscript_path = work_dir / "config_import.sh"
    rcscript_path.write_text(OPNSENSE_CONFIG_IMPORT_SCRIPT, encoding="utf-8")

    # Copy stock image to work dir (Docker script reads it)
    stock_copy = work_dir / "stock.qcow2"
    shutil.copy2(stock_qcow2, stock_copy)

    output_path = work_dir / BASE_QCOW2_FILENAME

    logger.info("Running Docker injection (syshook script)...")
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{work_dir}:/work",
            DOCKER_IMAGE_NAME,
            "/work/stock.qcow2",
            "/work/config_import.sh",
            f"/work/{BASE_QCOW2_FILENAME}",
            OPNSENSE_CONFIG_IMPORT_PATH,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Docker injection failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    # Clean up intermediate files, keep only the output
    stock_copy.unlink(missing_ok=True)
    rcscript_path.unlink(missing_ok=True)

    logger.info(f"Base image at {output_path}")
    return output_path


class OpnsenseTemplateManager:
    """Manages OPNsense base template and per-eval config ISOs.

    The base template is a stock OPNsense nano image with an rc.d script
    that reads config from CDROM at boot. It is built once via Docker and
    cached as a Proxmox template.

    Per-eval config (config.xml + dns_whitelist.conf) is delivered as a
    tiny ISO attached as ide2 CDROM to each cloned VM.
    """

    logger = getLogger(__name__)

    def __init__(
        self,
        async_proxmox: AsyncProxmoxAPI,
        node: str,
        image_storage: str,
        task_wrapper: TaskWrapper,
        qemu_commands: QemuCommands,
        storage_commands: LocalStorageCommands,
    ):
        self.async_proxmox = async_proxmox
        self.node = node
        self.image_storage = image_storage
        self.task_wrapper = task_wrapper
        self.qemu_commands = qemu_commands
        self.storage_commands = storage_commands

    def find_base_template_tag(self) -> str:
        """Return the deterministic tag for the OPNsense base template."""
        return base_template_tag()

    async def ensure_template(self) -> str:
        """Ensure the OPNsense base template exists.

        Returns the template tag. Config-independent — the same base
        template is shared by all OPNsense VMs regardless of their
        domain whitelist.
        """
        tag = base_template_tag()

        if await self._template_exists(tag):
            self.logger.info(f"OPNsense base template already exists ({tag})")
            return tag

        self.logger.info(f"Creating OPNsense base template ({tag})")
        await self._create_template(tag)
        return tag

    def generate_config_iso(
        self,
        subnet: SubnetConfig,
        static_maps: Sequence[tuple[str, str, str | None]] = (),
    ) -> Path:
        """Generate a config ISO for an OPNsense VM.

        Returns the path to a temporary ISO file. The caller is
        responsible for cleanup (e.g. passing it to qemu_commands
        which uploads and then deletes).

        Args:
            subnet: The OPNsense-managed SubnetConfig.
            static_maps: Sequence of (mac, ipv4, hostname) tuples for
                DHCP static mappings on the LAN.
        """
        config_xml = generate_config_xml(subnet, static_maps)
        unbound_conf = generate_unbound_whitelist_conf(subnet)

        iso = pycdlib.PyCdlib()
        iso.new(
            interchange_level=3,
            joliet=3,
            rock_ridge="1.12",
            vol_ident="OPNCONF",
        )

        for iso_name, joliet_name, rr_name, content in [
            ("CONFIG_X", "config.xml", "config.xml", config_xml),
            (
                "DNS_WHIT",
                "dns_whitelist.conf",
                "dns_whitelist.conf",
                unbound_conf,
            ),
        ]:
            content_bytes = content.encode("utf-8")
            buffer = BytesIO(content_bytes)
            iso.add_fp(
                buffer,
                len(content_bytes),
                f"/{iso_name}",
                joliet_path=f"/{joliet_name}",
                rr_name=rr_name,
            )

        temp_file = tempfile.NamedTemporaryFile(
            delete=False, suffix=".iso"
        )
        iso.write_fp(cast(BinaryIO, temp_file))
        temp_file.close()
        return Path(temp_file.name)

    async def find_base_template_vm_id(self) -> int:
        """Find the VM ID of the OPNsense base template."""
        tag = base_template_tag()
        existing_vms = await self.qemu_commands.list_vms()
        for vm in existing_vms:
            if (
                "tags" in vm
                and "template" in vm
                and vm["template"] == 1
                and "inspect" in vm["tags"].split(";")
                and tag in vm["tags"].split(";")
            ):
                return vm["vmid"]
        raise ValueError(
            f"No OPNsense base template found with tag {tag}. "
            "Was ensure_template() called in task_init?"
        )

    async def _template_exists(self, tag: str) -> bool:
        """Check if a template VM with this tag exists."""
        existing_vms = await self.qemu_commands.list_vms()
        for vm in existing_vms:
            if (
                "tags" in vm
                and "template" in vm
                and vm["template"] == 1
                and "inspect" in vm["tags"].split(";")
                and tag in vm["tags"].split(";")
            ):
                return True
        return False

    async def _create_template(self, tag: str) -> None:
        """Create the OPNsense base template via Docker (rc.d script injection only)."""
        loop = asyncio.get_event_loop()

        # Step 1: Ensure Docker image is built
        with trace_action(self.logger, TRACE_NAME, "building Docker injector image"):
            await loop.run_in_executor(None, _ensure_docker_image)

        # Step 2: Ensure stock OPNsense qcow2 is cached locally
        with trace_action(self.logger, TRACE_NAME, "ensuring stock OPNsense image"):
            stock_path = await loop.run_in_executor(None, _ensure_stock_image_local)

        # Step 3: Inject rc.d script via Docker
        with trace_action(self.logger, TRACE_NAME, "Docker rc.d script injection"):
            base_path = await loop.run_in_executor(
                None, _docker_inject_rcscript, stock_path
            )

        # Step 4: Upload base qcow2 to Proxmox
        with trace_action(
            self.logger,
            TRACE_NAME,
            "uploading base image to Proxmox",
        ):
            await self.storage_commands.upload_file_to_storage(
                file=base_path,
                content_type="import",
                filename=BASE_QCOW2_FILENAME,
            )

        # Step 5: Create template VM from uploaded image
        with trace_action(
            self.logger, TRACE_NAME, f"creating OPNsense base template {tag}"
        ):
            await self._create_opnsense_template_vm(tag)

        # Clean up the base image (stock is cached for reuse)
        base_path.unlink(missing_ok=True)

    async def _create_opnsense_template_vm(self, tag: str) -> None:
        """Create a Proxmox template VM from the uploaded base qcow2."""
        vm_id = await self.qemu_commands.find_next_available_vm_id()

        async def do_create() -> None:
            await self.async_proxmox.request(
                "POST",
                f"/nodes/{self.node}/qemu",
                json={
                    "vmid": vm_id,
                    "name": f"inspect-opnsense-{tag}",
                    "node": self.node,
                    "cpu": "host",
                    "memory": 2048,
                    "cores": 2,
                    "ostype": "other",
                    "scsi0": f"{self.image_storage}:0,"
                    + f"import-from={LOCAL_STORAGE}:import/"
                    + f"{BASE_QCOW2_FILENAME},"
                    + "format=qcow2,cache=writeback",
                    "scsihw": "virtio-scsi-single",
                    "serial0": "socket",
                    "vga": "std",
                    "start": False,
                    "agent": "enabled=0",
                },
            )

        await self.task_wrapper.do_action_and_wait_for_tasks(do_create)

        # Tag it
        async def update_tags() -> None:
            await self.async_proxmox.request(
                "POST",
                f"/nodes/{self.node}/qemu/{vm_id}/config",
                json={"tags": f"inspect;{tag}"},
            )

        await self.task_wrapper.do_action_and_wait_for_tasks(update_tags)

        # Convert to template
        async def convert_to_template() -> None:
            await self.async_proxmox.request(
                "POST", f"/nodes/{self.node}/qemu/{vm_id}/template"
            )

        await self.task_wrapper.do_action_and_wait_for_tasks(convert_to_template)

        # Wait until it's actually a template
        @tenacity.retry(
            wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
            stop=tenacity.stop_after_delay(VM_TIMEOUT),
            retry=tenacity.retry_if_result(lambda x: x is False),
        )
        async def is_template() -> bool:
            current_config = await self.async_proxmox.request(
                "GET",
                f"/nodes/{self.node}/qemu/{vm_id}/config?current=1",
            )
            return current_config.get("template") == 1

        await is_template()

        self.logger.info(f"OPNsense base template created: VM {vm_id}, tag {tag}")
