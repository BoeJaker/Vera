# 32 · Cluster E2E Encryption

**Status: Phase 1 built (WireGuard provider), Nebula provider stubbed, Phases
2–3 designed.** The mesh lives in `networking/netsec_capabilities.py`
(`netsec.mesh.*`) with a panel at `/netsec/panel` (Workers → Provision → Mesh).
It builds on pieces that already exist: the secprov backing services
(`provisioning/security_provision_capabilities.py`), the Enroll flow
(`provisioning/enroll_capabilities.py` — step-ca trust + SSH user-CA + cert-only
logins), the canonical exec SSH store, and the netgraph firewall caps
(`networking/netgraph_capabilities.py`).

## What's built

`netsec.mesh.*` — providers, config(.save), candidates, members, join, sync,
status, leave. A **pluggable provider seam** (`MeshProvider`): `wireguard`
(fully working — in-kernel, keys generated on-host over SSH, `wg syncconf`
hot-reload, `wg show dump` status) and `nebula` (experimental — CA + host-cert
minting implemented on the Vera host; the on-host config push is the remaining
step). Config in Redis `vera:netsec:mesh`. Join is enrolment-aware: cross-refs
the enrol store by IP, records a `cert` flag, and — only when `enforce` is on —
refuses non-enrolled hosts (tolerant by default). All work runs over
`exec.ssh.run`, so any reachable exec-store host can be pulled onto the mesh.

---

## Goal

1. Every Vera control-plane and data connection (Redis, Postgres, Neo4j,
   Chroma, Garage, Ollama, worker RPC) can run over an encrypted overlay.
2. Only **authenticated, enrolled** devices participate — enrolment (step-ca
   certificate) is the admission ticket.
3. **Tolerant first**: the overlay is additive. Nothing breaks the day it goes
   in; existing LAN paths keep working until enforcement is switched on
   deliberately, per service.
4. SSH stops being password-bootstrapped: after enrolment Vera connects with
   its step-ca-minted SSH certificate only (this part already works — Enroll
   panel → "Vera SSH identity" → Mint).

## Approach — WireGuard full mesh, Vera as the coordination plane

WireGuard over Nebula/Tailscale because:

- wg is in-kernel on PVE nodes and Debian guests; LXC guests only need the
  host module; Docker hosts run it on the host and containers ride host
  routing. No third-party coordination server.
- Vera already has everything a coordinator needs: root SSH to every node
  (exec store), `pct exec` into CTs, a Redis registry, and an enrolment
  authority (step-ca). Private keys are generated **on the device** over the
  already-authenticated SSH channel and never leave it; only public keys
  travel.
- step-ca stays the single identity root (TLS + SSH certs). wg keys are
  *transport* keys, bound to an enrolment record — revoking the enrolment
  removes the peer.

Nebula's cert-based mesh is the closest alternative, but it introduces a
second CA and a second enrolment concept; rejected to keep one identity model.

## Module: `vera/networking/netsec_capabilities.py` (`netsec.mesh.*`)

| Capability | What it does |
|---|---|
| `netsec.mesh.providers` | List installed backends + the active one. |
| `netsec.mesh.config(.save)` | Read/set provider, subnet (`10.88.0.0/16`), listen port, iface, `enforce`. Provider/subnet locked once members exist. |
| `netsec.mesh.candidates` | Exec-store hosts not yet on the mesh, each flagged enrolled (cert) or not. |
| `netsec.mesh.members` | Members with overlay IP, enrolment flag, last state. |
| `netsec.mesh.join` | Install provider over SSH, gen identity **on the host**, allocate overlay IP, register peer, resync everyone. Enrolment checked; refused only under `enforce`. |
| `netsec.mesh.sync` | Re-render + hot-reload the peer set (WireGuard `syncconf`, no restart). |
| `netsec.mesh.status` | Per-member interface up? peers, handshake age, rx/tx. "Enrolled but never handshaken" = warning, not failure. |
| `netsec.mesh.leave` | Tear the interface down on a host + resync the rest. |
| `netsec.policy.apply` | **Phase 3 (planned):** ask OPA which edges are allowed; render to nftables / PVE firewall via `netgraph.edge.allow/deny`. |

Provider seam: implement `MeshProvider` (ensure_installed / gen_identity /
apply / status / teardown) and register in `_PROVIDERS`. Adding a backend is a
class, not a rewrite — that's the "configurable backend" the design calls for.

UI: `/netsec/panel` → Workers → Provision → **Mesh** sub-tab (config bar,
members table with sync/leave, candidates table with join). A future pass adds
an overlay-address column to the Connections pane.

## Backend choice — WireGuard vs the alternatives

Default is **raw WireGuard coordinated by Vera**: Vera already is an
authenticated coordinator (root SSH + step-ca + Redis), so no external control
server is needed and the identity root isn't duplicated. **Nebula** is the
strongest alternative and ships as a selectable provider — its built-in host
firewall (security groups) is the reason to prefer it once per-edge policy
matters (Phase 3). NetBird / Headscale (self-hosted Tailscale) were considered
but add a heavier control plane to run; they could be added as providers later.
Tailscale-hosted and ZeroTier-hosted were rejected for depending on a
third-party coordinator.

## Rollout phases

- **Phase 0 (done in the panels now):** deploy OpenBao / step-ca / lldap / OPA
  from Provision → Security; enroll guests; mint Vera's SSH cert; flip host
  records to `auth=cert`.
- **Phase 1 — overlay, tolerant:** join nodes + hosts to the mesh. All
  services still listen on LAN; Vera *prefers* overlay IPs where both ends
  have them (host records gain `mesh_ip`). A dead overlay link falls back to
  the LAN address — log it, don't fail it.
- **Phase 2 — enforcement, per service:** rebind sensitive services
  (Redis/Postgres/Neo4j/Garage admin) to the overlay IP; publish Docker ports
  on the wg address instead of `0.0.0.0`; PVE firewall drops LAN ingress to
  those ports. One service at a time, with a revert switch.
- **Phase 3 — policy + rotation:** OPA decides peer↔peer edges (rendered into
  wg `AllowedIPs` + nftables); wg key rotation on re-enrol; enrolment
  revocation removes the peer within one `mesh.sync`.

## Non-goals / notes

- Not a VPN for users — it is machine-to-machine only; the harness UI stays on
  its existing ingress.
- ESP32 mesh devices (documentation/14) are out of scope — they can't run wg;
  their transport security stays at the protocol layer.
- The dual SSH stores (exec vs enroll) should converge before Phase 2 so the
  cert-auth flag and mesh_ip live in one place.

## Screenshots

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
