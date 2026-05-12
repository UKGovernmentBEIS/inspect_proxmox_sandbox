# Inspect Proxmox Sandbox

## Purpose

This plugin for [Inspect](https://inspect.aisi.org.uk/) allows you to use virtual machines, 
running within one or more [Proxmox](https://www.proxmox.com/products/proxmox-virtual-environment/overview) instances, as [sandboxes](https://inspect.aisi.org.uk/sandboxing.html).

## Installing

Add this using [uv](https://github.com/astral-sh/uv),

```
uv add git+ssh://git@github.com/UKGovernmentBEIS/inspect_proxmox_sandbox.git
```

or with [Poetry](https://python-poetry.org/),

```
poetry add git+ssh://git@github.com/UKGovernmentBEIS/inspect_proxmox_sandbox.git
```

## Requirements

This plugin assumes you already have one or more Proxmox instances set up, and that you have admin access to them.

Your Proxmox instance(s) must allow additional storage types in `local` from the default.
You can run this on your Proxmox node to configure them:

```bash
pvesh set /storage/local -content iso,vztmpl,backup,snippets,images,rootdir,import
```

SDN requires you to configure dnsmasq, see the [Proxmox SDN documentation](https://pve.proxmox.com/pve-docs/chapter-pvesdn.html#pvesdn_install_dhcp_ipam). Note, the commands on that page must be run on the Proxmox node, not your local machine.

If you don't already have a Proxmox instance, see [CONTRIBUTING.md](CONTRIBUTING.md#setting-up-a-proxmox-instance-for-testing) for supported setup paths (local Ubuntu 24.04 host, or EC2 with nested virtualization).

### Single Proxmox Instance

Set the following environment variables (e.g. in a [`.env`](https://dotenvx.com/docs/env-file) file):

```
PROXMOX_HOST=[IP address or domain name of the host]
PROXMOX_PORT=[port, e.g 8006]
PROXMOX_USER=[user, usually 'root']
PROXMOX_REALM=[authentication realm, usually 'pam' unless you have configured custom auth]
PROXMOX_PASSWORD=[password]
PROXMOX_NODE=[node name, usually 'proxmox']
PROXMOX_VERIFY_TLS=[1 = verify, 0 = do not verify]
PROXMOX_IMAGE_STORAGE=[storage pool for VM disk images, usually 'local-lvm']
```

### Multiple Proxmox Instances

To run evals across multiple Proxmox servers, create a JSON config file and point to it with `PROXMOX_CONFIG_FILE`:

```bash
export PROXMOX_CONFIG_FILE=/path/to/instances.json
```

**instances.json**:
```json
{
  "instances": [
    {
      "instance_id": "proxmox-1",
      "pool_id": "ubuntu-ami-123",
      "host": "10.0.1.10",
      "port": 8006,
      "user": "root",
      "user_realm": "pam",
      "password": "secret",
      "node": "pve1",
      "verify_tls": false
    },
    {
      "instance_id": "proxmox-2",
      "pool_id": "ubuntu-ami-123",
      "host": "10.0.1.11",
      "port": 8006,
      "user": "root",
      "user_realm": "pam",
      "password": "secret",
      "node": "pve2",
      "verify_tls": false
    }
  ]
}
```

Instances with the same `pool_id` form a pool. Each eval sample acquires one instance from its pool, uses it exclusively, and releases it back when done. Concurrency is automatically limited to the total number of instances.

## Configuring

Here is a full example sandbox configuration. 

Note that some of the fields (e.g. subnets) are tuples, so the trailing comma is vital 
if there is only a single item in the tuple.

Most tools use only the first sandbox, so you should list the one you want the agent to operate from first.

Virtual machines must have the [qemu-guest-agent](https://pve.proxmox.com/wiki/Qemu-guest-agent) installed, unless they are not sandboxes. 
At least one VM in the configuration must be a sandbox.

```python
sandbox=SandboxEnvironmentSpec(
    "proxmox",
    ProxmoxSandboxEnvironmentConfig(
        # Storage pool for VM disk images. Defaults to PROXMOX_IMAGE_STORAGE env var
        # or "local-lvm" if not set.
        image_storage="local-lvm",

        # When using PROXMOX_CONFIG_FILE with multiple instances, set this to select
        # which pool to use (must match a pool_id in the config file).
        # Not needed for single-instance setups.
        # instance_pool_id="ubuntu-ami-123",

        vms_config=(
            VmConfig(
                # A virtual machine that this provider will install and configure automatically.
                vm_source_config=VmSourceConfig(
                    built_in="ubuntu24.04" # currently supported: "ubuntu24.04", "debian13", "kali2025.4"; see schema.py
                ),
                name="romeo", # name is optional, but recommended - it will be shown in the Proxmox GUI and registered as the Inspect sandbox environment identifier. Must be a valid DNS name.
                ram_mb=512, # optional, default is 2048 MB
                vcpus=4, # optional, default is 2. No attempt is made to check that this will fit in the Proxmox host.
                uefi_boot=True, # optional, default is False. Generally only needed for Windows VMs.
                is_sandbox=False, # optional, default is True. A virtual machine that is not a sandbox; the qemu-guest-agent need not be installed.
                disk_controller="scsi", # optional, default will be SCSI. Can also use "ide" for older VM images.
                nic_controller="virtio", # optional, default will be VirtIO. Can also use "e1000" for older VM images.
                cpu="host", # optional, default "host". The qemu CPU model (e.g. "host", "qemu64", "x86-64-v2"). Older guest kernels (notably FreeBSD/pfSense) can panic on nested virtualization with "host"; use "qemu64" for those.
                firewall=True, # optional, default is False. Enables the Proxmox firewall on all NICs for VM isolation.
                # If you have more than one VNet, assign the VM to the VNet via nics.
                # You can assign more than one, to give the VM more than one network interface.
                # If you leave this blank, your VM will be assigned to the first VNet.
                nics=(
                    VmNicConfig(
                        # This alias *must* match the alias in one of the VnetConfigs
                        vnet_alias="my special vnet",
                        # Specifying a MAC address is optional - only needed if you
                        # are doing fancy things with DHCP in your eval, or if you
                        # want to assign a static IP address
                        mac="00:16:3d:1d:eb:a0",
                        # Specifying a static IPv4 address is optional. If provided,
                        # a DHCP static mapping (host reservation) will be created.
                        # Note: requires a MAC address to be specified as well.
                        # Please read the notes in README.md for Proxmox server patching requirements
                        ipv4=ip_address("192.168.20.10")
                    ),
                )
                # extra_proxmox_native_config = dict() TODO
            ),
            # A virtual machine from a local OVA, which will be uploaded from here to the Proxmox server.
            VmConfig(
                vm_source_config=VmSourceConfig(
                    ova=Path("./tests/oVirtTinyCore64-13.11.ova")
                ),
                os_type="win10" # optional, default "l26".
            ),
            # A virtual machine to clone from an existing template VM.
            # This is *not recommended* since it is dependent on configuring a 
            # customised Proxmox instance that contains the template VM before
            # the eval start.
            VmConfig(
                vm_source_config=VmSourceConfig(
                    existing_vm_template_tag="java_server"
                ),
            ),
            # A virtual machine that is connected to a predefined VNET.
            # This is *not recommended* since it is dependent on configuring a
            # customised Proxmox instance that contains SDN configurations before
            # the eval start.
            VmConfig(
                vm_source_config=VmSourceConfig(
                    built_in="ubuntu24.04"
                ),
                nics=(
                    VmNicConfig(
                        # If you reference a pre-existing VNET here, and
                        # set sdn_config=None in the ProxmoxSandboxEnvironmentConfig,
                        # it will look for the VNET alias in the existing Proxmox SDN.
                        vnet_alias="existing vnet alias",
                    ),
                )
            ),
            # A virtual machine with no network access.
            VmConfig(
                # ... snip ...           
                nics=()
            ),
        ),
        # You will need a separate SDN per sample, or the VMs will be able to see each other
        # IP ranges *must* be distinct, unfortunately.
        # If you don't care about any of this, you can set this field to the string "auto"
        # and you will get an IP range somewhere in 192.168.[2 - 253].0/24
        sdn_config=SdnConfig(
            vnet_configs=(
                VnetConfig(
                    # You can leave subnets blank if you are handling IPAM yourself (e.g. with your own pfsense instance as a VM)
                    subnets=(
                        SubnetConfig(
                            cidr=ip_network("192.168.20.0/24"),
                            gateway=ip_address("192.168.20.1"),
                            # If you set snat=False, VMs will see each other but not the wider Internet.
                            snat=True,
                            dhcp_ranges=(
                                DhcpRange(
                                    start=ip_address("192.168.20.50"),
                                    end=ip_address("192.168.20.100"),
                                ),
                            ),
                        ),
                    ),
                    alias="my special vnet"
                ),
            ),
            # Set use_pve_ipam_dnsnmasq to True if you want your instances to be able to access the Internet
            use_pve_ipam_dnsnmasq=True,
        ),
    ),
)
```

### VM Names

It is recommended that you set the `name=` parameter for your defined VMs. This name serves two purposes:
- It will be displayed in the Proxmox web interface
- It will be the identifier you use to reference the VM in Inspect (e.g., `sandbox("vm_name")`)

You should avoid setting the same name for multiple VMs as this will cause conflicts in how Inspect references your VMs; later VMs with the same name will overwrite earlier ones in the sandbox name mapping. While both VMs would still be created in Proxmox, only the last one would be accessible through its name in Inspect. If you omit the name parameter, the VM will be registered in Inspect using its dynamically-generated ID, as `vm_<id>`.

> Note: The (first) sandbox VM is automatically named `default` internally, so you can always access it with `sandbox("default")`, regardless of any custom name you might set for it.


### Static IP Address Assignment

By default, VMs receive IP addresses from the DHCP range specified in the subnet configuration. However, you can assign static IP addresses to VMs by specifying both a MAC address and an IPv4 address in the `VmNicConfig`:

```python
nics=(
    VmNicConfig(
        vnet_alias="my special vnet",
        mac="52:54:00:12:34:56",  # Required for static IP
        ipv4=ip_address("192.168.20.10")  # Static IP assignment
    ),
)
```

**How it works:**
- When both `mac` and `ipv4` are specified, the system creates a DHCP static mapping (host reservation) in Proxmox
- The VM will always receive the specified IP address when it boots
- The IP address must be within the subnet CIDR range but does not need to be within the DHCP range

**Requirements:**
- The `ipv4` field requires a `mac` address to be specified (validation will fail otherwise)
- The IP address must fall within one of the configured subnet CIDR ranges
- `use_pve_ipam_dnsnmasq` must be `True` in the SDN config
- The Proxmox server *must* be patched using the patch from https://lists.proxmox.com/pipermail/pve-devel/2025-November/076472.html

### Using Existing Proxmox VNETs (Advanced/Not Recommended)

**⚠️ WARNING**: This feature is intended for advanced users with specific integration requirements. For most use cases, you should let the sandbox manage its own network configuration using the standard `sdn_config` options.

If you have an existing Proxmox environment with pre-configured VNETs that you need to connect to, you can reference them by setting `sdn_config=None` and using the VNET aliases in your VM configurations:

```python
sandbox = SandboxEnvironmentSpec(
    type="proxmox",
    config=ProxmoxSandboxEnvironmentConfig(
        vms_config=(
            VmConfig(
                vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
                nics=(
                    VmNicConfig(
                        vnet_alias="existing-vnet-alias",  # Must match an existing VNET alias in Proxmox
                    ),
                ),
            ),
        ),
        sdn_config=None,  # Disable SDN creation - use existing VNETs only
    ),
)
```

Static IP address assignment is *not* supported with this feature.

## Using OVA files

Proxmox supports OVA import but not OVA export. It is possible to extract the disk images of VMs 
from a Proxmox server in qcow2 format (instructions for this can be found online).

Once you have the disk images locally, you can use the convenience script `src/proxmoxsandbox/scripts/ova/convert_ova.sh`
to convert it into an OVA.

This provider creates a template VM for every OVA-type VM specified in an eval.
Next time you run the eval, a linked clone of the template VM will be created.
This is for performance. If you change the OVA, as long as the filesize changes, 
a new template VM will be created. If you change the OVA but the filesize remains
the same, you should manually delete it from the Proxmox server.

These template VMs are *not* cleaned up because that needs to happen outside
the lifecycle of an Inspect eval. You need to do this manually at the moment.

## Windows VMs

Windows VMs are supported via the QEMU guest agent. To use a Windows VM:

1. Create a Windows VM template on your Proxmox server with the [QEMU guest agent](https://pve.proxmox.com/wiki/Qemu-guest-agent) installed and running
2. Convert it to a template and tag it with `inspect;<your-tag>`
3. Reference it in your eval config:

```python
VmConfig(
    vm_source_config=VmSourceConfig(
        existing_vm_template_tag="your-tag"
    ),
    os_type="win11",  # or "win10", "win8", etc. — see schema.py for all options
    uefi_boot=True,
    is_sandbox=True,
    ram_mb=8192,
)
```

The `os_type` field determines how commands are executed inside the VM. Windows types (any value starting with `w`) use batch scripts instead of shell scripts. The QEMU guest agent channel on Windows is less reliable than on Linux, so transient errors are automatically retried.

## Observing the VMs

Note, if you are having problems, then setting Inspect's `sandbox_cleanup=False` will be helpful.

### Logging in

If you want to log into a sandbox VM, the Proxmox UI lets you open a console window, but you might not know the password.

You can use the following command on the Proxmox server (open Datacenter -> Proxmox node -> Shell):

```bash
export PROXMOX_NODE=proxmox # change this if necessary
export VM_ID=101 # change this to the correct VM ID
export NEW_PASSWORD=Password2.0 # choose a password
export VM_USERNAME=ubuntu # change as appropriate
pvesh create "/nodes/$PROXMOX_NODE/qemu/$VM_ID/agent/exec" --command bash --command "-c" --command "echo $VM_USERNAME:$NEW_PASSWORD | chpasswd"
```

## Snapshot

QEMU, the virtualization library used by Proxmox, allows you to snapshot a running virtual machine, 
including the running processes. See [snapshots.py](./src/proxmoxsandbox/experimental/snapshots.py) for example tools that use this.

## Sample eval

See [ctf4.py](./src/proxmoxsandbox/experimental/ctf4.py) for an example capture-the-flag eval with:

- A VM for the agent
- A victim VM which the agent must hack into and obtain the root password

## Identifying created resources

Every VM created by this sandbox provider is tagged `inspect`. 
(Tags will also be duplicated if they exist on a VM already, for `existing_backup_name`- and `existing_vm_template_tag`-type VMs)

SDN zones have the pattern `[3 letters from eval task name][random 3 digits][z]`. VNets are similar and can be identified from their containing zone.

Some resources will persist after the eval is complete:

- the built-in VM feature creates a template VM `inspect-ubuntu24.04`
- the built-in VM feature creates a SDN zone called `inspvmz`
- uploaded OVAs are left in place
- cloud-init ISOs are left in place

Environment cleanup is partially implemented. There is no way to tag all the resources
created by a particular eval. Therefore the cleanup process for `inspect sandbox cleanup proxmox` 
will delete:

- all VMs tagged `inspect` 
- any SDN zones created with names matching the pattern above.

When using `PROXMOX_CONFIG_FILE`, cleanup runs against every instance in the config file.

## Versioning

The project follows [semantic versioning](https://semver.org/) and is aiming for a 1.0 release. Until then, backward-compatibility is not guaranteed.

## Large `write_file` fast path

For Linux guests, `write_file` payloads larger than 1 MiB use an ISO9660 hot-plug fast path that sidesteps the QEMU guest-agent ~60 KiB per-call write cap. The payload is packed into an ISO, uploaded to Proxmox storage, attached to a dedicated `sata5` CD-ROM slot (cold-added at clone time on every `is_sandbox` VM), then mounted and copied inside the guest. Falls back unconditionally to chunked QGA on any failure, so this is a pure optimisation.

### When the fast path is disabled

If a `write_file` call's fast path fails for any reason, the sandbox emits a `WARNING` log and disables the fast path **for that VM** for the rest of the sample. Subsequent large writes on the same VM go straight to the slower chunked-QGA path. Other VMs in the same sample are unaffected.

If you see this warning, things to check:

1. **VM config**: the fast path needs `sata5` populated as `none,media=cdrom`. If your VM template (`existing_vm_template_tag` path) already uses `sata5` for something else, the cold-add at clone time overwrites it — and if your eval relies on that pre-existing `sata5` content, you'll want to either move it to a different slot (`sata0`–`sata4`) or disable the fast path globally.
2. **Guest dmesg**: look for AHCI errors or `Can't open blockdev` messages. The kernel sometimes refuses optical-device opens after the first media-change; the fast path includes one host-side re-attach retry, so if you're hitting this warning it means the retry didn't recover either.
3. **Proxmox storage**: a full `local` storage pool will cause ISO upload to fail. Free space and retry.
4. **OS type**: the fast path is Linux only. Windows VMs always use chunked QGA.

To disable the fast path globally, set `ProxmoxSandboxEnvironment.ISO_WRITE_THRESHOLD_BYTES` to a value larger than your largest payload. To re-enable for a VM mid-sample, set `env._iso_fast_path_disabled = False` (rarely useful — if it failed once, it'll usually fail again).

## Feature Roadmap

- Proxmox server health and config check
- Normalize having a pfSense VM as the default route for networking
- Firewall off the SDN from the Proxmox server and from other SDNs
- Support cloud-init for VM definition
- Escape hatch for Proxmox API so you can specify arbitrary parameters during VM / SDN creation 

## Built-in VM image versions

The built-in VMs (`ubuntu24.04`, `debian13`, `kali2025.4`) pin specific upstream image URLs in `built_in_vm.py`. These are not auto-updated — when a new upstream release appears (e.g. a new Kali quarterly release), the URL, the `Literal` type in `schema.py`, and all references in tests and examples must be updated together.

## Tech debt

- Large OVA uploads use PycURL, because neither aiohttp nor httpx worked with large uploads
- Inconsistent use of task_wrapper and tenacity

## Developing

See [CONTRIBUTING.md](CONTRIBUTING.md)
