# 🐟 Babblefish — Vera's Universal Protocol Translator

> *"The Babel fish is small, yellow, leech-like, and probably the oddest thing in the
> universe… if you stick one in your ear you can instantly understand anything said to
> you in any form of language."* — The Hitchhiker's Guide to the Galaxy

Babblefish lets Vera (and the LLM behind it) **speak, hear, and hold a conversation in
arbitrary network protocols** through a registry of small, pluggable protocol modules.
Instead of hand-rolling raw bytes, an agent picks a protocol, sends a high-level request,
and gets a structured reply — and, when it needs to, keeps a live connection open and
*mimics a real client* so it can drop into an environment and operate there.

---

## 1. Files

| File | Purpose |
|------|---------|
| `modules.py` | The protocol-module framework, built-in modules, personas, and the stateful `ConnectionContext`. Stdlib-only. |
| `babblefish_capabilities.py` | The `babblefish.*` capability group + the console panel route + `register_ui`. |
| `babblefish_panel.html` | A self-contained 🐟 console for driving protocols by hand. |
| `__init__.py` | Re-exports the public surface of `modules.py`. |

Registered in `capability_orchestration.py`'s `_module_files` list. **Note:** cap files
are loaded by basename with no package context, so `babblefish_capabilities.py` imports
the framework via the *absolute* path `from Vera.vera.babblefish import modules as bf`
(a relative `from . import modules` would fail).

---

## 2. Core concepts

### Protocol module
Teaches Babblefish one language. Minimum contract:

```python
class ProtocolModule:
    name, aliases, transport("tcp"|"udp"), default_port, description, example
    def encode(self, request) -> bytes      # high-level request → wire bytes
    def decode(self, data: bytes) -> dict    # wire bytes → structured, readable
```

Optional **connection-context hooks** (this is what makes it more than a byte codec):

```python
    greets_first: bool                       # peer sends a banner on connect
    def contextualize(self, request, ctx)    # adapt an outgoing request using the session
    def on_response(self, ctx, decoded, raw) # update negotiated state from a reply
    def fingerprint(self, ctx) -> dict        # interrogate the peer over an open connection
```

### ConnectionContext (a live session)
A persistent connection to one peer that carries the context needed to speak a protocol
*properly over time*:

- **`state`** — negotiated facts: the greeting banner, HTTP cookies, a server version,
  advertised features, an auth token.
- **`profile`** — the persona (client fingerprint) it wears to mimic a real client.
- **`history`** — the exchanges so far.

`send()` runs the full loop: `contextualize → encode → send → recv → decode →
on_response`, so state accumulates across the conversation (e.g. a cookie set on request 1
is automatically sent on request 2).

### Persona (mimicry)
A named set of protocol defaults merged into outgoing requests. Built-ins: `vera` (honest
default), `curl`, `chrome`, `firefox`, `googlebot`. `learn_profile(name, fingerprint)`
derives a new persona from an observed environment so later traffic blends in. Personas
are the difference between *poking* a service and *speaking its native dialect*.

### Drop-in flow (`adopt`)
`open → fingerprint → (learn persona) → stay open`. One call understands an environment and
leaves Vera holding a ready, context-aware, mimicking session it can `send` on.

---

## 3. Capability reference (`babblefish.*`)

**Introspection / one-shot**
- `modules` — list the pluggable protocol modules. *Call this first.*
- `encode {protocol, request}` — dry-run: show the wire bytes, send nothing.
- `decode {protocol, data}` — parse raw bytes (hex/b64/text) into structure.
- `speak {protocol, host, port?, request, timeout?, transport?, profile?}` — one-shot
  connect → send → decode. `profile` applies a persona for a single request.
- `listen {host, port, protocol?, timeout?, transport?}` — receive-only banner grab.
- `probe {host, port?, timeout?}` — identify the protocol on a host (port → matching
  modules + banner; no port → a short bounded common-port list).

**Stateful sessions**
- `connect {protocol, host, port?, timeout?, transport?, profile?}` → `{session_id}`.
- `send {session_id, request, expect_reply?}` — contextualised send on an open session.
- `recv {session_id, timeout?}` — read without sending.
- `close {session_id}` / `sessions` — lifecycle + listing.

**Understand & adopt**
- `fingerprint {host, port?, protocol?, timeout?}` — environment profile (banner, server,
  version, features). Deeper than `probe`.
- `adopt {host, port?, protocol?, profile?}` — drop into an environment: open + fingerprint
  + auto-persona, session left **open** for `send`.

**Personas / extensibility**
- `profiles` — list personas.
- `learn_profile {name, fingerprint}` — derive & store a persona.
- `register_module {spec}` — teach a new protocol from a **declarative** JSON spec (no
  code): `{name, transport?, default_port?, framing?(line|raw), terminator?,
  request_template?("GET {key}"), response?(text|hex|lines)}`.

---

## 4. Built-in modules

| Module | Transport / port | Notes |
|--------|------------------|-------|
| `raw`   | tcp | Arbitrary bytes in/out — the escape hatch. |
| `line`  | tcp | Line protocols (SMTP/POP3/IRC/FTP); banner fingerprint. |
| `http`  | tcp / 80 | Cleartext HTTP/1.1. **Stateful**: persona headers, cookie carry, `Server` fingerprint, keep-alive in sessions. |
| `redis` | tcp / 6379 | RESP encode/decode; `PING`+`INFO` fingerprint. |
| `dns`   | udp / 53 | Query build + answer parse (A/AAAA/MX/TXT/NS/CNAME). |
| `whois` | tcp / 43 | Query + text record. |

Plus any number of **declarative** modules registered at runtime.

---

## 5. Adding a protocol

**Declarative (no code)** — best for line/length/http-framed protocols:
```json
{ "name": "memcache", "default_port": 11211, "framing": "line",
  "request_template": "get {key}", "response": "lines" }
```
Send it to `babblefish.register_module` and it's immediately in `babblefish.modules`.

**Code (full logic)** — subclass `ProtocolModule`, implement `encode`/`decode` (+ optional
context hooks), and `register(MyModule())` in `modules.py`.

---

## 6. Known limitations (today)

- **No TLS.** `http` is cleartext only; `https`/`smtps`/`redis-over-TLS` need a TLS module.
- **HTTP framing is idle-based**, not `Content-Length`/chunked-aware — large or streamed
  bodies may be truncated or need a longer idle window. Fine for headers/small bodies.
- **DNS answer parsing is byte-correct but unverified against a live resolver** in this
  environment (UDP/53 egress was filtered during development). Encode is verified.
- **Cookie/header handling is simplified** — one value per header name; multiple
  `Set-Cookie` collapse to the last.
- **No inbound/server mode** — Babblefish is a client; it cannot yet *listen* as a service
  and answer in a protocol.
- **Sessions are per-process, blocking sockets** (bounded to 64, 600 s idle TTL), driven off
  the event loop via `asyncio.to_thread`. No connection pooling or multiplexing.
- **Mimicry is application-layer only** — persona headers, not TLS/JA3 or TCP-stack
  fingerprints. It fools log-level inspection, not a real fingerprinting stack.

---

## 7. Roadmap — how to iterate

Ordered roughly by value ÷ effort.

**Near term**
1. **TLS transport.** Wrap sockets in `ssl` with SNI; add `https` (443) and a generic
   `starttls` upgrade step. Unlocks the majority of real-world services.
2. **Proper HTTP framing.** Honour `Content-Length` and `Transfer-Encoding: chunked`;
   optional gzip/deflate decode. Removes the idle-timeout guesswork.
3. **More modules:** MQTT, AMQP, Postgres/MySQL wire, SSH (banner + kex only), SMTP with a
   real `EHLO→AUTH→MAIL` state machine, Modbus/TCP, SNMP, NTP, TLS-ClientHello probe.
4. **Handshake state machines.** Let a module declare an ordered `handshake` (read banner →
   EHLO → capture features → AUTH) that `adopt` runs automatically, with `state`
   transitions (`unauth → authed`) gating which requests are legal.

**Mid term**
5. **Traffic learning & replay.** Observe a real exchange (pcap or a proxied session),
   derive a declarative module *and* a persona from it, then replay/parametrise it. Turns
   "I saw this protocol once" into a reusable module.
6. **Deeper mimicry.** JA3/JA4 TLS fingerprints, header **order** preservation, HTTP/2
   settings — move from "looks like curl in the logs" to "looks like Chrome to a
   fingerprinter."
7. **Server / listener mode.** `babblefish.serve {protocol, port, handler}` so Vera can
   *answer* in a protocol — honeypots, mock services, protocol bridges.
8. **Codec sharing with the rest of Vera.** Expose modules as `ide`/`exec`-callable codecs,
   and let the **netmap** and **mesh** subsystems hand Babblefish a discovered host:port to
   fingerprint and label automatically.

**Longer term / research**
9. **LLM-authored modules.** Given a spec/RFC snippet or a sample capture, have the coding
   cohort generate a full `ProtocolModule` (encode/decode + tests) and hot-register it —
   the declarative path is the safe subset of this.
10. **Session orchestration.** Multi-step "recipes" (login → poll → act) as saved,
    parameterised flows, surfaced as one-shot caps — Babblefish as an integration layer,
    not just a translator.
11. **Safety & governance.** Per-target allow/deny lists, rate limiting, an audit trail of
    every exchange, and a clear read-only vs. active-write distinction — important as the
    protocol surface (and thus blast radius) grows.

---

## 8. Who drives it

The **`protocol-linguist`** agent (networking cohort) is Babblefish's operator; the
**`network-engineer`** hands it wire-level work. See `agents.py` `DEFAULT_AGENTS`.
