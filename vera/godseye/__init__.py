"""vera.godseye — Godseye geospatial-intelligence globe, integrated into Vera.

Godseye (https://github.com/VrushankPatel/godseye) is a frontend-only React +
CesiumJS dashboard that renders live aircraft, satellites, seismic, weather,
maritime and CCTV feeds on a 3D globe. It stays a **separate upstream repo**:
Vera clones it at runtime into a git-ignored ``vendor/`` directory, builds it in
a throwaway Node container (so the host needs no toolchain) and serves the
resulting static bundle itself — nothing of Godseye's source is ever committed
to Vera.

``godseye_core`` holds the pure, app-free logic (asset path guard, build-log
state machine, git/docker argv builders, BYOK key handling) so it is
unit-testable without the orchestrator; ``godseye_capabilities`` wires it to the
capability, HTTP and UI surface.
"""
