"""Data models and schemas for the Proxmox sandbox configuration."""

import json
import os
from os import getenv
from pathlib import Path
from typing import Annotated, Literal, Optional, Tuple, TypeAlias, Union

from pydantic import BaseModel, Field, model_validator
from pydantic.networks import IPvAnyAddress, IPvAnyNetwork
from pydantic_extra_types.mac_address import MacAddress


class DhcpRange(BaseModel, frozen=True):
    """
    Represents a DHCP range with start and end IP addresses.

    Attributes:
        start: The starting IP address of the DHCP range
        end: The ending IP address of the DHCP range
    """

    start: IPvAnyAddress
    end: IPvAnyAddress

    def _to_proxmox_format(self) -> str:
        return f"start-address={self.start},end-address={self.end}"


class SubnetConfig(BaseModel, frozen=True):
    """
    Configuration for a subnet within a virtual network.

    Attributes:
        cidr: The subnet in CIDR notation
        gateway: The gateway IP address for the subnet
        snat: Whether source NAT is enabled for this subnet
        dhcp_ranges: DHCP ranges configured for this subnet
    """

    cidr: IPvAnyNetwork
    gateway: IPvAnyAddress
    snat: bool
    dhcp_ranges: Tuple[DhcpRange, ...]


class VnetConfig(BaseModel, frozen=True):
    """
    Configuration for a virtual network.

    Attributes:
        alias: A human-readable alias for the virtual network.
            The alias is also used in this configuration to link each VM in the Vnet.
        subnets: Subnet configurations for this virtual network.
    """

    alias: Optional[
        # original regex (?^i:[\(\)-_.\w\d\s]{0,256}) but that's not especially
        # Python-compatible
        Annotated[str, Field(pattern=r"[()-_.[a-z][A-Z][0-9]\s]{0,256}")]
    ] = None
    subnets: Tuple[SubnetConfig, ...] = ()


class SdnConfig(BaseModel, frozen=True):
    """
    Software-defined networking configuration.

    Attributes:
        vnet_configs: Configurations for VNets.
        use_pve_ipam_dnsnmasq: Whether to use Proxmox VE's built-in IPAM and DNSmasq
            Set to False if you are using e.g. your own pfsense instance for IPAM
            (recommended)
    """

    vnet_configs: Tuple[VnetConfig, ...]
    use_pve_ipam_dnsnmasq: bool = True


SdnConfigType: TypeAlias = Union[SdnConfig, Literal["auto"], None]


class VmSourceConfig(BaseModel, frozen=True):
    """
    Configuration for the source of a virtual machine.

    Exactly one source type must be specified.

    Attributes:
        existing_vm_template_tag: Clone VM from existing Proxmox template with this tag
        ova: Create VM from this OVA file in the local (not Proxmox) filesystem.
        built_in: Use this provider's built-in VM template (currently "ubuntu24.04"
            is supported)
    """

    existing_vm_template_tag: str | None = None
    ova: Path | None = None
    # Ubuntu 24.04 is supported because an OVA is publicly available from a reliable
    # source.
    # From Proxmox 9.0 onwards, qcow2 and raw are also supported, allowing Debian 13,
    # Kali, and others.
    built_in: Literal["ubuntu24.04", "debian13", "kali2025.3"] | None = None

    @model_validator(mode="after")
    def _validate_single_source(self) -> "VmSourceConfig":
        set_sources = [
            name
            for name, value in {
                "existing_vm_template_tag": self.existing_vm_template_tag,
                "ova": self.ova,
                "built_in": self.built_in,
            }.items()
            if value is not None
        ]

        if len(set_sources) != 1:
            raise ValueError(
                "Exactly one source must be set. "
                + f"Found {len(set_sources)}: {', '.join(set_sources) or 'none'}"
            )

        return self


class VmNicConfig(BaseModel, frozen=True):
    """
    Configuration for a virtual machine network interface.

    Attributes:
        vnet_alias: The alias of the VNet to connect to. This can be either:
            - An alias defined in the sdn_config
            - An existing VNET alias in Proxmox (when sdn_config is None)
        mac: The MAC address for the network interface (optional)
    """

    vnet_alias: str
    mac: Optional[MacAddress] = None


class VmConfig(BaseModel, frozen=True):
    """
    Configuration for a virtual machine.

    Attributes:
        vm_source_config: The source configuration for the VM
        name: The name of the VM (optional). Must be a valid DNS name.
        ram_mb: RAM allocation in megabytes (default: 2048)
        vcpus: Number of virtual CPUs (default: 2)
        nics: Network interface configurations (optional)
        is_sandbox: if True, the VM will show up as a sandbox.
            It must have the qemu-guest-agent installed
        uefi_boot: if True, the VM will boot in UEFI mode. In theory, this is already
            specified by OVA, but Proxmox doesn't seem to respect it.
        disk_controller: The disk controller type. If unset, defaults to "scsi"
        nic_controller: The NIC controller type. If unset, defaults to "virtio".
            This is applied to all virtual network interfaces.
        os_type: The OS type. If unset, defaults to "l26". Only for OVA. See
            https://pve.proxmox.com/wiki/Manual:_qm.conf for more details

    Note on nics configuration:
    - If set, the VM will be connected to these VNets (one interface per VNet)
    - If set as the empty tuple (), the VM will not have any NICs
    - If left as the default None:
        If the vm_source_config is existing_vm_template_tag,
            the NICs will be left as configured in the template.
        If the vm_source_config is ova or built_in, it will be connected to the first
            VNet.
    """

    vm_source_config: VmSourceConfig
    name: Optional[str] = None
    ram_mb: Optional[int] = 2048
    vcpus: Optional[int] = 2
    nics: Optional[Tuple[VmNicConfig, ...]] = None
    is_sandbox: bool = True
    uefi_boot: bool = False
    disk_controller: Optional[Literal["scsi", "ide"]] = None
    nic_controller: Optional[Literal["virtio", "e1000"]] = None
    os_type: Optional[
        Literal[
            "l24",
            "l26",
            "other",
            "solaris",
            "w2k",
            "w2k3",
            "w2k8",
            "win10",
            "win11",
            "win7",
            "win8",
            "wvista",
            "wxp",
        ]
    ] = "l26"


class ProxmoxInstanceConfig(BaseModel, frozen=True):
    """
    Configuration for a single Proxmox instance.

    Attributes:
        instance_id: Unique identifier for this instance
        pool_id: Image/AMI identifier - instances with the same pool_id share a queue
            Examples: AMI ID, S3 path, or "default" for blank instances
        host: The hostname or IP address of the Proxmox server
        port: The port number for the Proxmox API, usually 8006
        user: The username for Proxmox authentication
        user_realm: The authentication realm for the Proxmox user
        password: The password for Proxmox authentication
        node: The name of the Proxmox node
        verify_tls: Whether to verify the Proxmox server's TLS certificate
    """

    instance_id: str
    pool_id: str
    host: str
    port: int
    user: str
    user_realm: str
    password: str
    node: str
    verify_tls: bool


def _load_instances_from_env_or_file() -> Tuple[ProxmoxInstanceConfig, ...]:
    """
    Load Proxmox instance configurations from file or environment variables.

    Priority order:
    1. PROXMOX_CONFIG_FILE environment variable (JSON file)
    2. Legacy single-instance environment variables (backwards compatibility)

    Returns:
        Tuple of ProxmoxInstanceConfig objects
    """
    # Priority 1: Read from PROXMOX_CONFIG_FILE environment variable
    config_file = getenv("PROXMOX_CONFIG_FILE")
    if config_file and os.path.exists(config_file):
        with open(config_file) as f:
            data = json.load(f)
            instances_data = data.get("instances", [])
            return tuple(ProxmoxInstanceConfig(**inst) for inst in instances_data)

    # Priority 2: Backwards compatibility - single instance from env vars
    host = getenv("PROXMOX_HOST")
    if host:
        return (
            ProxmoxInstanceConfig(
                instance_id="default",
                pool_id="default",
                host=host,
                port=int(getenv("PROXMOX_PORT", "8006")),
                user=getenv("PROXMOX_USER", "root"),
                user_realm=getenv("PROXMOX_REALM", "pam"),
                password=getenv("PROXMOX_PASSWORD", "password"),
                node=getenv("PROXMOX_NODE", "proxmox"),
                verify_tls=getenv("PROXMOX_VERIFY_TLS", "1") == "1",
            ),
        )

    # No configuration found - return empty tuple
    return ()


class ProxmoxSandboxEnvironmentConfig(BaseModel, frozen=True):
    """
    Configuration for a Proxmox sandbox environment.

    Attributes:
        instance_pool_id: Which pool to use for this sample (must match a pool_id in
            PROXMOX_CONFIG_FILE or defaults to "default" for legacy single-instance mode)
        sdn_config: Software-defined networking configuration
        vms_config: Configurations for virtual machines
        host: (Legacy) The hostname or IP address of the Proxmox server
        port: (Legacy) The port number for the Proxmox API, usually 8006
        user: (Legacy) The username for Proxmox authentication
        user_realm: (Legacy) The authentication realm for the Proxmox user
        password: (Legacy) The password for Proxmox authentication
        node: (Legacy) The name of the Proxmox node
        verify_tls: (Legacy) Whether to verify the Proxmox server's TLS certificate
    """

    # Which pool to use (references pool_id in PROXMOX_CONFIG_FILE)
    instance_pool_id: str = "default"

    # Eval-specific configuration
    sdn_config: SdnConfigType = "auto"
    vms_config: Tuple[VmConfig, ...] = (
        VmConfig(vm_source_config=VmSourceConfig(built_in="ubuntu24.04")),
    )

    # Legacy single-instance fields (for backwards compatibility)
    host: str = Field(default_factory=lambda: getenv("PROXMOX_HOST", "localhost"))
    port: int = Field(default_factory=lambda: int(getenv("PROXMOX_PORT", "8006")))
    user: str = Field(default_factory=lambda: getenv("PROXMOX_USER", "root"))
    user_realm: str = Field(default_factory=lambda: getenv("PROXMOX_REALM", "pam"))
    password: str = Field(
        default_factory=lambda: getenv("PROXMOX_PASSWORD", "password")
    )
    node: str = Field(default_factory=lambda: getenv("PROXMOX_NODE", "proxmox"))
    verify_tls: bool = Field(
        default_factory=lambda: getenv("PROXMOX_VERIFY_TLS", "1") == "1"
    )