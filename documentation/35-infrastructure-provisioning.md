# 35 · Infrastructure and Provisioning

This subsystem turns machines and networks into usable Vera capacity. It brings
together Foundry, build workers, Proxmox, provisioning, networking, network
monitoring, and remote-host registration. Docker operations remain covered in
[Docker](13-docker.md); this guide covers the layer beneath and around it.

## Components

| Area | Responsibility |
|---|---|
| Foundry | Define and run repeatable machine/service build workflows |
| Build | Arduino, PlatformIO, Python, and general build-job execution |
| Provisioning | Bootstrap hosts, PXE assets, installation state, and handoff |
| Proxmox | Discover clusters/guests and obtain console access |
| Networking | Resolve machines, addresses, and service reachability |
| Netmon | Observe availability and network-level health |
| Remote | Store reusable remote connection definitions |

## Provisioning lifecycle

1. Register or discover the physical/virtual host.
2. Inspect current identity, addresses, storage, and boot state.
3. Produce a reviewed provisioning/build plan.
4. Stage artifacts without changing the running target.
5. Execute the explicitly authorized install or build step.
6. Verify boot, network identity, required services, and Vera registration.
7. Record the resulting versions and artifact identities.

Discovery and planning should be read-only. Power, reboot, PXE, disk layout,
firmware, and guest lifecycle operations can interrupt or destroy workloads and
need an exact target plus confirmation.

## State ownership

Proxmox owns guest lifecycle; the guest owns its operating system; Docker owns
containers; Vera owns connection metadata and orchestration records. Do not
copy an external system's mutable state into Vera and then treat both copies as
authoritative. Persist stable IDs and refresh volatile status.

Build artifacts must be content-addressed or versioned. A successful command
without the expected artifact is a failed build. Keep source revision, toolchain
version, target, configuration, checksum, and logs with releasable artifacts.

## Troubleshooting order

1. Resolve the target ID to the intended host/guest.
2. Test network reachability and required ports.
3. Verify credentials and privilege.
4. Check storage and artifact availability.
5. Inspect the build/provisioning job state and logs.
6. Verify the target after execution rather than trusting dispatch success.

Common failure modes include DHCP/DNS drift, stale Proxmox tickets, mismatched
host keys, full build disks, unavailable toolchains, wrong board profiles, and
rebooting a different machine after an address changed.

## Source map

- `vera/foundry/` — Foundry plans, UI, and orchestration.
- `vera/build/` — build runners and status.
- `vera/provisioning/` — provisioning/PXE logic.
- `vera/proxmox/` — cluster, guest, and console integration.
- `vera/networking/` and `vera/netmon/` — identity and health.
- `vera/remote/` — reusable remote connection definitions.

<!-- VERA:AUTO:screenshots START -->
<!-- VERA:AUTO:screenshots END -->

<!-- VERA:AUTO:capabilities START -->
<!-- VERA:AUTO:capabilities END -->
