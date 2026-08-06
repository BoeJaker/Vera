# Vera OS Provisioning ("Foundry") — design, roadmap & outstanding items

_Last updated: 2026-08-06. Owner: infra. Status: design agreed → awaiting go on Phase 1._

This doc has two parts:
- **Part A** — the OS-provisioning system you asked for (design + phased plan + open decisions).
- **Part B** — the outstanding backlog from recent work (things to pick up later).

---

## Part A — OS Provisioning system ("Foundry")

### Goal (your ask, restated)
A provisioning server, running mainly on the Proxmox box, that installs operating systems onto **Proxmox VMs, Proxmox CTs, Docker, and physical/networked devices** — with SSH certs, network drives (SMB/NFS), and the private mesh network **baked in**, plus **hardening** and **security monitoring**, and optional **file-server / docker-swarm / distributed-compute** membership. Users drive the whole thing from a web UI. FreeIPA (identity/CA) and Gitea (script/config store) are the backbone.

### What Vera already gives us (compose, don't rebuild)
- **Proxmox**: `proxmox.lxc.create` (+auto_enroll), `guest.clone/destroy/exec/ip/action`, `nextid`, `storage.content`, `fw.*`, `console.ticket`.
- **Docker**: `docker.image.ensure/build`, `run`, `worker.spawn`, `stack.catalog/deploy`, `hosts.*`, `ps`, `exec`.
- **Feature seam**: `provision.components` + `provision.deploy(.start/.done)` + `provision.component.status/stop` — a component-deploy mechanism we hang feature bundles off.
- **Enrolment**: `enroll.guest` (SSH cert + FreeIPA + mesh in one call, Proxmox-native/no-password mode) and `autoenroll` (closed-loop, watch new guests).
- **Identity/secrets/net**: FreeIPA (users/hosts/certs/**Dogtag CA**), OpenBao (vault, now with KMS auto-unseal), **WireGuard mesh** (`netsec.mesh.*`), lldap fallback.
- **Storage**: Proxmox storage (ISO/vztmpl/rootdir) + Garage S3 (blobs) + `pxstore.*`.
- **View**: the new **Estate map** (Workers & Ollama → Estate) already visualises hosts + their provisioning/security posture — the natural monitoring surface for this.

### The model — three separable axes (your "separate OSes from features")
A provisioning job = **Target × Base image × Features[]**.

**1. Target** (where the OS goes)
| Target | How | Client type |
|---|---|---|
| Proxmox **CT** | `proxmox.lxc.create` from an LXC template | "Proxmox client" |
| Proxmox **VM** | new `proxmox.vm.create` (cloud-img + cloud-init) — **gap to build** | "Proxmox client" |
| **Docker** | `docker.run` / `stack.deploy` / `worker.spawn` | container/swarm |
| **Physical device** | **PXE/netboot autoinstall** — **gap to build** | "installer client" |
| **Thin client** | PXE boots a tiny shell-OS that attaches to a target VM's desktop/SSH — **gap, Phase 3** | "thin client" |

**2. Base image — one catalog, per-type adapters** (answers your "3+ image types" question)
Keep **one versioned catalog**; each entry declares its `type` and the catalog knows how to import/serve it per type:

| type | used by | stored where | import via |
|---|---|---|---|
| `cloudimg` (qcow2/raw + cloud-init) | VMs, physical | Proxmox storage / Garage | download + checksum |
| `lxc-template` (vztmpl) | CTs | Proxmox `vztmpl` | `pveam` / upload |
| `docker` | Docker/swarm | registry (Gitea/registry) | `docker pull` / `image.ensure` |
| `iso` | VMs, physical | Proxmox `iso` | download + checksum |
| `ipxe` (netboot profile) | physical | Foundry HTTP/TFTP | generated |

Catalog entry: `{id, os, version, type, arch, source_url, sha256, size, location, added, is_latest}`. Versioning = keep N versions + a `latest` pointer per (os,type). Big blobs live in Garage or Proxmox storage; the catalog is the index + the import/serve logic. Caps: `foundry.image.list/add/import/version/delete`.

**3. Features — composable, toggleable bundles** (applied post-install, idempotent, via `guest.exec`/SSH, scripts versioned in **Gitea**)
| Feature | What it bakes in |
|---|---|
| `enrol` *(baseline, always)* | SSH **cert** trust + FreeIPA host/user join |
| `mesh` | WireGuard private network membership |
| `file-server` | Samba + NFS share host (exports) |
| `file-client` | mounts the shared drives (SMB/NFS), autofs |
| `hardening` | CIS-ish: sshd policy, host firewall, unattended-upgrades, fail2ban, sysctl, disable-root-login |
| `security-monitoring` | auditd + a light agent shipping to syslog / a Wazuh-lite collector |
| `docker-swarm` | install docker + join/init the swarm |
| `distributed-compute` | join the Vera worker/Ollama compute cluster (`worker.init`/`ollama` node) |

The UI presents these as toggles; the backbone (FreeIPA certs, mesh keys, share creds) is wired automatically so the user just ticks boxes.

### Blueprints — versioned, reusable Infrastructure-as-Code  ✅ built
A **blueprint** is a declarative manifest describing a whole estate — a list of
nodes, each `= target × image × features × resources` with a per-node `count`
(so "3 web CTs + a file-server" is one document). Blueprints are **versioned on
every save** (prior versions snapshotted, roll back to any), **re-applied**
idempotently by fanning out to `foundry.provision` (with a `dry_run` plan mode),
and **exported as a portable YAML manifest** (`foundry/v1`) you can commit to
git/Gitea — the IaC file — and `import` back. This is what makes Foundry a
"versioned, reusable infra setup tool like Docker/Terraform" rather than a
one-shot installer. Caps: `foundry.blueprint.save/list/get/apply/delete/export/
import`. Example manifest:
```yaml
foundry_blueprint:
  apiVersion: foundry/v1
  name: web-swarm
  nodes:
    - {name: web, target: ct, image_id: debian-12-lxc-template, count: 3,
       features: [enrol, mesh, hardening], node: corp, cores: 1, memory: 1024, disk: 8}
```

### Provisioning server ("vera-foundry")
A dedicated **Proxmox CT** on the .200 box hosting: netboot stack (**dnsmasq proxy-DHCP** — answers *only* PXE so it's safe on the existing no-DHCP LAN — + TFTP + **iPXE** + HTTP), the autoinstall/cloud-init/preseed generator (driven by the chosen features), and the image import workers. Gitea stores feature scripts; Garage/Proxmox storage holds image blobs; FreeIPA issues the certs.

### Web UI ("Foundry" panel)
Lives in **Workers & Ollama → Provision** (or its own sub-tab next to Estate): pick **target** → pick **OS + version** from the catalog → toggle **features** → **Provision** → live progress; plus a **Catalog** manager (add / import / version images) and a **Physical devices** view (PXE waiting-room: MACs seen, assign a profile). The **Estate map** already shows the result.

### Phased plan
- **Phase 0 — design** ✅ (this doc).
- **Phase 1 — Catalog + VM/CT/Docker provisioning + feature bundles** *(low risk; mostly orchestrates existing caps)*. Build: `foundry.image.*` catalog; `proxmox.vm.create` (cloud-init); `foundry.provision {target, image, features[]}`; the 8 feature bundles as `provision.components`; the Foundry UI. **Deliverable:** from the web UI, stand up a CT / VM / Docker host from a cataloged image with chosen features, auto-enrolled into FreeIPA + mesh.
- **Phase 2 — Physical devices via PXE.** Stand up `vera-foundry` CT (dnsmasq proxy-DHCP + TFTP + iPXE + HTTP); autoinstall (Debian/Ubuntu autoinstall, AlmaLinux kickstart) driven by the same feature set. **Deliverable:** PXE-boot a laptop → OS installed + features + enrolled.
- **Phase 3 — Thin client + swarm + distributed-compute polish.** iPXE thin-OS booting into a remote VM desktop (SPICE/RDP) or SSH; harden the swarm + compute-join bundles; catalog auto-refresh/versioning.

### Decisions (confirmed 2026-08-06)
1. **Build order:** ✅ **Phase 1 first** — VM/CT/Docker + catalog + features (no physical yet).
2. **Provisioning server home:** ✅ **new dedicated CT `vera-foundry` on .200**.
3. **Seed OSes:** ✅ **Debian 12, Ubuntu 24.04, AlmaLinux 9, Alpine, Arch, Kali, Windows (stub)**.
4. **PXE / LAN:** ⚠️ **discuss further — lean to a dedicated Proxmox-internal VLAN, NOT proxy-DHCP on the main LAN.**

**PXE risk note (why plain proxy-DHCP on the main LAN is wrong for this estate):**
- proxy-DHCP only supplies *boot* info; it needs a *real* DHCP server to hand out the IP. This LAN is **static / no-DHCP**, so PXE clients would get no IP and never boot → proxy-DHCP alone is insufficient here.
- Running a full DHCP on the main LAN risks **rogue-DHCP** conflicts for other devices.
- Netboot serves the OS + autoinstall config (which can carry join tokens/secrets) over unauthenticated TFTP/HTTP.
- Machines left on network-boot could **accidentally** pick up the provisioning menu.

**→ Recommended Phase-2 approach:** a **dedicated Proxmox-internal provisioning bridge/VLAN** (Proxmox supports internal VLANs) with a *scoped* DHCP+TFTP+iPXE on `vera-foundry`, isolated from the main LAN. Physical laptops attach to that VLAN only during provisioning; VMs/CTs get a NIC on that bridge while installing. Mitigate accidental boots with a chainload menu that defaults to local-disk unless the MAC is approved (waiting-room). Finalize switch/VLAN details at Phase 2.

---

## Part B — Outstanding backlog (pick up later)

### From the identity / cutover / integrations work
1. **OpenBao: move prod off dev mode.** KMS auto-unseal tooling + UI are done (`identity.openbao.autounseal.setup`, boot hook). Still to do (user-triggered, consequential): run `bao operator init` on a **persistent** OpenBao, register the unseal keys via the Provision → Identity "KMS auto-unseal" card, confirm boot auto-unseal. Re-init discards dev-mode data.
2. **Cert renewal automation.** Vera's TLS cert is now FreeIPA-issued (`vera.vera.int`, SAN incl. IP .138) and verified. FreeIPA certs **expire** — add a renewal job (re-`identity.cert.request` + swap + restart) before expiry, and/or `certmonger` on the host. Rollback backup: `~/.vera/tls/*.bak.20260805-023745`.
3. **Grafana embed** still shows broken assets under the sub-path proxy — Grafana uses absolute `/public/...` paths. Fix = set Grafana `root_url` / `serve_from_sub_path` for its integration, or open it in a new tab. (n8n, Home Assistant redirect, Portainer scheme are fixed.)
4. **Ollama unprivileged-LXC repair.** CTs 126/129–132 are unprivileged with `/root` owned by an unmapped uid (`nobody`) → key-install fails (no passwordless SSH), and unprivileged LXCs can't run WireGuard without host NET_ADMIN/module → mesh join fails. FreeIPA + FQDN now succeed. Fix on the container: host-side chown of the mapped `/root`, or make it privileged / recreate.
5. **Formalise the git merge.** All this work lives in the clone `Vera-integrations` (branch `integrations-ca-ldap`, latest `1aa28f3`) and is deployed to prod's working tree. Do a clean `git merge` into `agentic-loop-improvements-2`/`main` once the other stream's tree is clean.
6. **Minor:** `identity.openbao.status` `auth_methods` lists response-envelope keys — should read `.data`.

### From the Estate map
7. **Verify Estate map rendering in a browser.** Data endpoints, wiring and JS syntax check out, but the visual layout (host chips, curved edges, pulses) wasn't verified live. Confirm it looks right; adjust layout if host chips/edges overlap at scale.

### The big one
8. **OS Provisioning "Foundry"** — Part A above. Phase 1 is ready to start pending the 4 open decisions.

---

_Deploy note: prod is native tmux; reload code with `POST /sys/dev/restart {"confirm":true}` (safe, ~2s). Edit over SMB `\\192.168.0.138\boejaker\Vera`. Off-repo Proxmox helper: `//192.168.0.138/boejaker/.vera-proxmox/pve_connect.py`._
