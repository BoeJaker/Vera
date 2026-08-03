"""vera.integrations — the Integrations Hub.

A first-class, integration-centric layer over Vera's existing app-mount reverse
proxy, operator, MCP catalog, SSH/exec host store and identity/PKI/mesh stack.
Each external service is one *integration record* that can be embedded, operated
by interaction, called by API, driven over MCP, or reached by SSH — each behind a
per-integration access toggle that is ENFORCED (see ``policy.require_access``),
not merely hidden in the UI.

``policy`` holds the pure, dependency-free logic (kind specs, URL resolution and
the access gate) so it is unit-testable without Redis or the orchestrator;
``integrations_capabilities`` wires it to the capability/HTTP surface.
"""
