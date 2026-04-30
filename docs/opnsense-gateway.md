# OPNsense as a domain-whitelist gateway

This document describes how to use OPNsense as a per-VNet gateway VM that
restricts egress traffic to a set of whitelisted domains. See
`docs/domain-whitelist-gateway-spec.md` for the motivation and requirements.

## How it works

Each VNet that needs domain filtering gets two networks:

1. **WAN VNet** — `snat=True`, gives the gateway VM internet access via
   Proxmox's built-in SNAT.
2. **LAN VNet** — `snat=False`. Sandbox VMs live here. Their only route out
   is the OPNsense gateway.

The OPNsense VM has one NIC on each VNet. It runs:

- NAT masquerade from LAN → WAN
- Unbound DNS resolver on LAN (so sandbox VMs can resolve names)
- pf firewall with an FQDN alias containing the whitelisted domains
- A default-deny rule that blocks all LAN egress not matching the alias

```
Sandbox VM (10.0.2.100)
    │
    ▼  default gw = 10.0.2.1
OPNsense (LAN: 10.0.2.1  ←→  WAN: 10.0.1.50/DHCP)
    │  NAT masquerade + FQDN filter
    ▼
Proxmox SNAT (10.0.1.1)
    │
    ▼
Internet
```

## OPNsense nano image

Use the **nano** image (not the DVD installer). It's a pre-installed disk image
that boots directly — no installation wizard.

- Download: `https://mirror.ams1.nl.leaseweb.net/opnsense/releases/25.1/OPNsense-25.1-nano-amd64.img.bz2`
- Compressed: 494 MB. Raw: 3.0 GB. Qcow2: 2.2 GB.
- Convert: `bunzip2 OPNsense-25.1-nano-amd64.img.bz2 && qemu-img convert -f raw -O qcow2 OPNsense-25.1-nano-amd64.img opnsense.qcow2`
- Default login: `root` / `opnsense`

### VM settings

| Setting   | Value                      | Why                                                                          |
| --------- | -------------------------- | ---------------------------------------------------------------------------- |
| `cores`   | 2                          | Minimum practical                                                            |
| `memory`  | 2048                       | FreeBSD claims ~2 GB for buffers; struggles with less                        |
| `scsihw`  | `virtio-scsi-single`       | Performance                                                                  |
| `serial0` | `socket`                   | Creates UNIX socket for serial console                                       |
| `vga`     | `std`                      | **Not** `serial0`. FreeBSD comconsole needs a real UART, not redirected VGA. |
| `net0`    | `virtio,bridge=<wan-vnet>` | WAN interface (becomes vtnet0)                                               |
| `net1`    | `virtio,bridge=<lan-vnet>` | LAN interface (becomes vtnet1)                                               |

## config.xml

OPNsense uses a single `/conf/config.xml` for all configuration. A working
reference config is at `src/proxmoxsandbox/scripts/experimental/opnsense_config.xml`.

### Sections that vary per deployment

The reference config is a complete, working file. For integration, these are the
sections that need to be templated (everything else is boilerplate):

**Interfaces** — WAN IP method and LAN IP/subnet:

```xml
<interfaces>
  <wan><if>vtnet0</if><ipaddr>dhcp</ipaddr>...</wan>
  <lan><if>vtnet1</if><ipaddr>10.0.2.1</ipaddr><subnet>24</subnet></lan>
</interfaces>
```

**DHCP** — LAN range, gateway, DNS server:

```xml
<dhcpd><lan>
  <range><from>10.0.2.50</from><to>10.0.2.200</to></range>
  <gateway>10.0.2.1</gateway>
  <dnsserver>10.0.2.1</dnsserver>
</lan></dhcpd>
```

**FQDN alias** — the domain whitelist:

```xml
<OPNsense><Firewall><Alias version="1.0.1"><aliases>
  <alias uuid="...">
    <name>whitelisted_domains</name>
    <type>host</type>
    <updatefreq>300</updatefreq>
    <content>ifconfig.me
oracle.com
api.ipify.org</content>
  </alias>
</aliases></Alias></Firewall></OPNsense>
```

**Firewall rules** — reference config has floating rules (processed first,
priority 200000) and interface rules:

Floating rules (quick, on LAN ingress):

1. Pass DNS UDP to OPNsense (`lanip:53`)
2. Pass DNS TCP to OPNsense (`lanip:53`)
3. Block all TCP to OPNsense (`lanip`) — logged
4. Block all UDP to OPNsense (`lanip`) — logged

Interface rules (LAN):

5. Allow ICMP from LAN (diagnostics)
6. Allow traffic from LAN to `whitelisted_domains` alias
7. Block all other LAN egress — logged

### Boilerplate sections

These don't change per deployment but are required:

- `<sysctl>` — FreeBSD tuning defaults
- `<system>` — hostname, user/group, timezone, SSH, serial console, web GUI
- `<unbound>` — DNS resolver (listen on LAN, outgoing on WAN)
- `<nat>` — automatic outbound NAT

## Gotchas

### Interface mapping is backwards

OPNsense defaults to WAN=vtnet1, LAN=vtnet0 — the opposite of what you'd expect
if you assign NICs in WAN-first order. The config.xml must explicitly set
`<wan><if>vtnet0</if>` and `<lan><if>vtnet1</if>`.

### LAN VNet must have no Proxmox subnet

If you create a Proxmox subnet on the LAN VNet, dnsmasq and IPAM will serve
DHCP on the LAN bridge, competing with OPNsense's DHCP server and advertising
the wrong gateway (the SDN bridge IP instead of OPNsense's LAN IP). The fix
is to declare the LAN VNet with no subnet at all (`subnets=()`) — the bridge
is still created as a L2 switch, but Proxmox doesn't run any L3 services on it.

### Auto-import from ISO does not work via opnsense-importer

The `opnsense-importer -b` script (run during every boot) only auto-scans
devices in interactive mode. In boot mode it just waits 7 seconds for a keypress,
then bootstraps from the default template if `/conf/config.xml` is absent.

The solution is a custom `rc.syshook.d/early/` script that mounts the CDROM
and copies `config.xml` before OPNsense reads it — see "Config injection" section.

### No wildcard domain support

The `host` alias type resolves FQDNs but does not support `*.example.com`. Possible
workarounds:

- URL Table alias pointing to a periodically-updated list of IPs
- Unbound DNS override to resolve wildcard patterns to known IPs
- Use the OPNsense API post-boot to create aliases programmatically

This is an open problem for the integration.

### FreeBSD shell is tcsh (not bash)

OPNsense's root shell is tcsh (FreeBSD ships tcsh as `/bin/csh`). Bash-style
redirections like `2>/dev/null` cause "Ambiguous output redirect" errors. Use
`command >& /dev/null` (tcsh syntax) or drop into `/bin/sh -c '...'` for
Bourne-compatible redirection.

### FQDN refresh

The alias refresh interval is set by `<updatefreq>` (seconds). Default in our
config: 300 (5 minutes). OPNsense's configd daemon resolves the FQDNs and updates
the pf table. If DNS is unreachable, stale IPs remain — fail-closed with stale data.

## Interacting with OPNsense

### Serial console (from Proxmox host)

The serial port appears as a UNIX socket at `/var/run/qemu-server/{vmid}.serial0`.

Copy `src/proxmoxsandbox/scripts/experimental/opn_serial.py` onto the proxmox host and then:

```bash
python3 opn_serial_5.py 100 "ifconfig vtnet0" "pfctl -sr"
```

This handles login (root/opnsense), menu navigation (option 8 = shell), and
command execution.

### SSH (from LAN — blocked by default)

SSH is enabled in config.xml but **blocked from LAN by floating pf rules**.
It is accessible from WAN or the serial console for operator debugging.

The root password is randomized on each template creation (bcrypt hash of a
`secrets.token_urlsafe(16)` value). The plaintext is logged via Python's
`logging.getLogger().info()` during `generate_config_xml()`. To find it in
the Inspect eval log:

```bash
inspect log dump logs/<log-file>.eval | jq -r \
  '.samples[].events[] | select(.event=="logger") | .message.message' \
  | grep "root password"
```

### Useful commands inside OPNsense

| Command                                      | Purpose                             |
| -------------------------------------------- | ----------------------------------- |
| `pfctl -sr`                                  | Show all firewall rules             |
| `pfctl -t whitelisted_domains -T show`       | Show resolved IPs in the FQDN alias |
| `sockstat -l -4 \| grep 53`                  | Verify Unbound DNS is listening     |
| `cat /var/log/filter/latest.log \| tail -20` | View firewall log (blocked/allowed) |
| `configctl filter reload`                    | Force-reload firewall rules         |

## Config injection

The nano image uses FreeBSD's UFS2 filesystem, which Linux mounts read-only.
Config injection is split into two parts:

1. **Static base image** (one-time): A boot script is injected into the stock
   OPNsense image via Docker + [UFS2Tool](https://github.com/SvenGDK/UFS2Tool).
   This creates a Proxmox template that is shared by all OPNsense VMs regardless
   of their domain whitelist.

2. **Per-eval config ISO**: `config.xml` and `dns_whitelist.conf` are packaged
   into a tiny ISO (~72 KB) using pycdlib and attached as an IDE CDROM (`ide2`)
   to each cloned VM. The boot script mounts the CDROM and copies the files
   before OPNsense reads its config.

### Boot script (rc.syshook.d/early)

The base image contains a shell script at
`/usr/local/etc/rc.syshook.d/early/09-config-import` that runs during
OPNsense's early boot, before configd and before networking. It:

1. Finds the CDROM device (`/dev/cd0`, `/dev/cd1`, or `/dev/acd0`)
2. Mounts it with `mount_cd9660`
3. Copies `config.xml` → `/conf/config.xml`
4. Copies `dns_whitelist.conf` → `/usr/local/etc/unbound.opnsense.d/`
5. Unmounts

This works because FreeBSD's GENERIC kernel (used by OPNsense nano) has
`cd9660` compiled in, and the IDE CDROM driver is built-in.

### Base image creation (Docker, one-time)

The Docker container (`opnsense-injector`) injects only the boot script into
the stock OPNsense image. It uses UFS2Tool to write to the FreeBSD UFS2
filesystem:

| Step                            | Time  |
| ------------------------------- | ----- |
| Docker build                    | ~30s  |
| Download + convert stock image  | ~90s  |
| Inject boot script via UFS2Tool | ~20s  |
| Upload 2.2 GB to Proxmox API   | ~8s   |
| Create template VM              | ~10s  |
| **Total**                       | **~2.5m** |

The base template is tagged `inspect;opnsense-base-{hash}` where the hash
covers the stock image URL and the boot script content. If a matching
template already exists in Proxmox, the entire pipeline is skipped.

### Per-eval steps

Each eval run generates a config ISO and clones the base template. No Docker
needed.

| Step                                    | Time   |
| --------------------------------------- | ------ |
| Check base template exists              | <1s    |
| Generate config ISO (pycdlib)           | <1s    |
| Upload ISO to Proxmox                   | <1s    |
| Clone template + attach ISO + configure | ~5s    |
| OPNsense boot                           | ~60s   |
| **Total**                               | **~70s** |

The ISO contains `config.xml` and `dns_whitelist.conf` (ISO 9660 Level 3 +
Rock Ridge + Joliet, ~72 KB). It is uploaded as
`vm-{id}-opnsense-config.iso` and attached as `ide2` CDROM.

### Alternatives considered and rejected

| Approach                               | Why not                                                |
| -------------------------------------- | ------------------------------------------------------ |
| guestfish / libguestfs                 | UFS2 mounted read-only even with libguestfs            |
| Linux kernel UFS2 write                | Requires kernel recompilation, unreliable              |
| Debian `makefs` package                | 2 GiB file size limit, OPNsense image is 3 GiB         |
| Boot + serial console inject           | Works but slow (~60s), fragile PTY automation          |
| opnsense-confgen ISO import            | Importer in boot mode (`-b`) doesn't auto-scan devices |
| Per-config Docker injection            | Requires Docker at eval time, can't pre-upload image   |
| opnsense-vm-images (build from source) | Requires FreeBSD, can't run in Linux Docker            |
| opnsense-bootstrap on FreeBSD          | Heavy, converts entire FreeBSD to OPNsense             |

### Docker injector

The Docker image and entrypoint are at:

- `scripts/experimental/opnsense_injector/Dockerfile`
- `scripts/experimental/opnsense_injector/inject_config.sh`

```bash
# Build the injector image (once)
docker build -t opnsense-injector \
  -f src/proxmoxsandbox/scripts/experimental/opnsense_injector/Dockerfile \
  src/proxmoxsandbox/scripts/experimental/opnsense_injector/

# Inject boot script into stock image
docker run --rm -v /path/to/workdir:/work opnsense-injector \
  /work/stock.qcow2 /work/config_import.sh /work/base.qcow2 \
  /usr/local/etc/rc.syshook.d/early/09-config-import
```

### Option B — on the Proxmox host via SSH

Not tested. SCP the boot script to the host, run UFS2Tool + `qemu-img` there.

- Pros: fast; no large transfer
- Cons: requires SSH access beyond the REST API; UFS2Tool must be present
  on host; couples the client to the host's shell environment

## Integration into inspect_proxmox_sandbox (implemented)

OPNsense is integrated via `SubnetConfig(vnet_type="opnsense")` on a VNet's
subnet. When a subnet has `vnet_type="opnsense"`, an OPNsense gateway VM is
**auto-generated** — the user only declares the SDN topology and agent VMs.
Docker is needed only for the one-time base image build.

### Schema

```python
# schema.py — SubnetConfig with vnet_type discriminator
class SubnetConfig(BaseModel, frozen=True):
    cidr: IPvAnyNetwork
    gateway: IPvAnyAddress
    snat: Optional[bool] = None
    dhcp_ranges: Tuple[DhcpRange, ...]
    vnet_type: Literal["proxmox", "opnsense"] = "proxmox"
    domain_whitelist: Optional[Tuple[str, ...]] = None
    # vnet_type="proxmox": snat required, domain_whitelist forbidden
    # vnet_type="opnsense": snat forbidden, domain_whitelist required
```

The OPNsense subnet is declared on the LAN VNet:

```python
VnetConfig(
    alias="lan",
    subnets=(
        SubnetConfig(
            cidr=ip_network("10.0.2.0/24"),
            gateway=ip_address("10.0.2.1"),
            vnet_type="opnsense",
            domain_whitelist=("ifconfig.me", "api.ipify.org"),
            dhcp_ranges=(DhcpRange(...),),
        ),
    ),
),
```

`infra_commands` auto-generates an OPNsense VM with WAN+LAN NICs. The
WAN VNet is auto-detected as the first VNet with `snat=True`.

Static IP assignments for VMs on the OPNsense LAN use the standard
`VmNicConfig(mac=..., ipv4=...)` mechanism. These are collected before
OPNsense boots and baked into config.xml as `<staticmap>` entries.

See `src/proxmoxsandbox/experimental/opnsense_eval.py` for the full example.

### How it works

| Phase            | What happens                                                                            |
| ---------------- | --------------------------------------------------------------------------------------- |
| `task_init`      | Detect `SubnetConfig(vnet_type="opnsense")` in sdn_config → `ensure_template()`        |
|                  | Check for base template tagged `inspect;opnsense-base-{hash}`                           |
|                  | If missing: create via Docker (inject boot script only), convert to template            |
| `sample_init`    | Auto-generate OPNsense VM → clone base template → config ISO (pycdlib) → ide2 → start  |
|                  | Then create + start agent VMs.                                                          |
| `sample_cleanup` | Delete cloned VMs + config ISOs + eval SDN (standard cleanup)                           |

### Static base template

One base template is shared by all OPNsense VMs regardless of their domain
whitelist. It is tagged `inspect;opnsense-base-{hash}` where the hash covers
the stock image URL and the boot script content. The template is created once
and reused across all evals.

```
Template tag: inspect;opnsense-base-91add32a
Template name: inspect-opnsense-opnsense-base-91add32a
```

### Config.xml generation

`_impl/opnsense.py:generate_config_xml()` parses the reference template
(`scripts/experimental/opnsense_config.xml`) as XML and updates:

- `<interfaces>` — LAN IP/subnet
- `<dhcpd>` — DHCP range, gateway, DNS server (all point to LAN IP)
- `<OPNsense><Firewall><Alias>` — domain whitelist content
- `<system><user><password>` — bcrypt hash of a random password (plaintext
  logged to Inspect eval log via `logger.info`)

`generate_unbound_whitelist_conf()` produces a separate Unbound config
(`dns_whitelist.conf`) that refuses DNS resolution for non-whitelisted
domains. Both files are delivered to the VM via the config ISO.

Everything else (sysctl, system, filter rules, NAT, Unbound base config)
stays as-is.

### Files

| File                                                 | Purpose                                                                                     |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `src/proxmoxsandbox/schema.py`                       | `SubnetConfig` with `vnet_type="opnsense"` discriminator                                    |
| `src/proxmoxsandbox/_impl/opnsense.py`               | `OpnsenseTemplateManager`, config.xml/ISO generation, Unbound DNS whitelist, Docker base build |
| `src/proxmoxsandbox/_impl/infra_commands.py`         | Auto-generates OPNsense VMs, config ISO hook, static map collection                         |
| `src/proxmoxsandbox/_impl/qemu_commands.py`          | Generic `post_clone_hook` on `create_and_start_vm()` (no OPNsense knowledge)                |
| `src/proxmoxsandbox/_proxmox_sandbox_environment.py` | Detects OPNsense subnets in `ensure_vms()`                                                  |
| `src/proxmoxsandbox/experimental/opnsense_eval.py`   | PoC eval                                                                                    |

### LAN VNet: OPNsense-managed subnet

The LAN VNet declares `SubnetConfig(vnet_type="opnsense")`. This subnet is
**not created in Proxmox** (no dnsmasq or IPAM on the LAN bridge).
OPNsense is the sole DHCP server, DNS resolver, and gateway on that VNet.

The WAN VNet still has a Proxmox-managed subnet with SNAT and DHCP ranges
(dnsmasq serves OPNsense its WAN IP).

### Cleanup behaviour

- `inspect sandbox cleanup proxmox` deletes eval VMs and SDN zones, but
  preserves templates (both built-in and OPNsense).

### Gotchas discovered during implementation

#### Findings from escape testing

- **OPNsense API (port 443):** Now blocked by floating pf rules.
  Previously reachable but returned 401.
- **Proxmox API from agent (direct):** Not reachable at `10.0.1.1:8006`
  or `192.168.99.1:8006` from the agent VM. Curl reports:
  `Failed to connect to 10.0.1.1 port 8006 after 5002 ms: Timeout was reached`
  (EXIT=28). No route from the LAN bridge to these IPs.
- **Pivot through OPNsense to Proxmox:** Not possible — SSH is blocked
  by floating pf rules, so the agent cannot reach OPNsense's shell.
  (SSH is enabled in config.xml for operator debugging, but the firewall
  blocks port 22 from LAN.) OPNsense could not reach the Proxmox API
  at `10.0.1.1:8006` or `192.168.99.1:8006` anyway.

### Remaining open problems

1. **Wildcard domains.** Host aliases don't support `*.example.com`. Needs
   investigation into URL Table aliases or Unbound overrides.

2. **Boot ordering.** OPNsense takes ~60s to boot. The agent VM's cloud-init
   retries DHCP for 120s, which provides enough margin, but this is brittle.
   VMs are created sequentially — OPNsense first.

## Running the eval

```bash
set -a; source .env; set +a
uv run inspect eval src/proxmoxsandbox/experimental/opnsense_eval.py \
  --no-sandbox-cleanup --model mockllm/model
```

Resources created by the provider:

| Resource                                  | Details                                                    |
| ----------------------------------------- | ---------------------------------------------------------- |
| `inspect-ubuntu24.04`                     | Built-in template (pre-existing)                           |
| `inspect-opnsense-opnsense-base-{hash}`   | OPNsense base template (one-time, shared across evals)     |
| `opnsense-gw`                             | OPNsense clone (2 NICs: WAN + LAN) + config ISO on ide2   |
| `agent`                                   | Ubuntu 24.04 clone (1 NIC: LAN)                            |
| `vm-{id}-opnsense-config.iso`             | Per-eval config ISO (config.xml + dns_whitelist.conf)      |
| SDN zone `opnXXXz`                        | WAN VNet (10.0.1.0/24, SNAT, DHCP) + LAN VNet (no subnet) |

Agent VM network (from OPNsense DHCP):

- IP: 10.0.2.50, Gateway: 10.0.2.1, DNS: 10.0.2.1

**Expected domain filtering behaviour:**

| Domain              | Whitelisted | Result                                              |
| ------------------- | ----------- | --------------------------------------------------- |
| `ifconfig.me`       | Yes         | Returns public IP — allowed                         |
| `api.ipify.org`     | Yes         | Returns public IP — allowed                         |
| `google.com`        | No          | exit 6 (DNS refused) — blocked by Unbound whitelist |
| `1.1.1.1` (bare IP) | No          | exit 28 (timeout) — blocked by pf firewall          |

**Timing:**

- First run (Docker build + stock image download + base template creation): ~2.5 min
- Subsequent runs (base template cached, only config ISO + clone): ~1.5 min

**Cloud-init note:** The agent VM template (ubuntu24.04) is built on the
static SDN with internet access, so cloud-init packages (including
qemu-guest-agent) are already installed. When the clone boots on the LAN
behind OPNsense, cloud-init re-runs but only needs DHCP — no package
downloads required.
