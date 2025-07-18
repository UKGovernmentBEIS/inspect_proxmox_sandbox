"""Data models and schemas for the Proxmox sandbox configuration."""

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
    # Kali does not have such an OVA. There is no other way to upload a VM image to
    # Proxmox 8.3.x. Hence, Kali support here would require Kali to provide an OVA.
    # The same goes for Debian.
    built_in: Literal["ubuntu24.04"] | None = None

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


class ProxmoxSandboxEnvironmentConfig(BaseModel, frozen=True):
    """
    Configuration for a Proxmox sandbox environment.

    Attributes:
        host: The hostname or IP address of the Proxmox server
        port: The port number for the Proxmox API, usually 8006
        user: The username for Proxmox authentication, 'root' unless you have configured
            custom auth
        user_realm: The authentication realm for the Proxmox user, 'pam' unless you have
            configured custom auth
        password: The password for Proxmox authentication
        node: The name of the Proxmox node, usually 'proxmox'
        verify_tls: Whether to verify the Proxmox server's TLS certificate.
            1 = verify, 0 = do not verify
        sdn_config: Software-defined networking configuration
            "auto": Create a simple SDN with a single subnet.  The IP addresses will not
                be predictable as it depends on what subnets already exist.
            None: No SDN will be created. VMs can reference existing VNETs in Proxmox
                by their aliases. This is an advanced feature and not recommended for
                normal use.
            SdnConfig: Custom SDN configuration
        vms_config: Configurations for virtual machines
    """

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

    sdn_config: SdnConfigType = "auto"
    vms_config: Tuple[VmConfig, ...] = (
        VmConfig(vm_source_config=VmSourceConfig(built_in="ubuntu24.04")),
    )
