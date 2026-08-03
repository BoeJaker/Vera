# Vera for VS Code

Brings Vera's IDE features into VS Code / code-server, and wires Vera into your
workspace's **Claude Code** as an MCP server so in-editor Claude Code can call
back into Vera (memory, fabric, IDE tools).

## Features

- **Vera sidebar** (activity-bar icon): live connection status, registered
  remote instances, the work queue, an **Enqueue Claude Code task** box for the
  current workspace, and an **Ask Vera** capability runner.
- **Vera: Connect this workspace** — downloads Vera's MCP bridge to
  `.vera/vera_mcp_bridge.py`, writes a project `.mcp.json`, and runs
  `claude mcp add vera …`. After this, Claude Code opened in the folder can use
  Vera's capabilities as tools.
- **Vera: Enqueue a Claude Code task for this workspace** — command-palette
  entry point for the same enqueue flow.
- **Vera: Install control tasks into this workspace** — merges a ready-made
  `.vscode/tasks.json` (start/stop/restart the server, force a Claude Code
  session sync, read/set arbitrary `.env` variables) built on top of
  `build.sh`/`build.ps1` and the `sys.dev.*`/`sys.env.*` capabilities. Safe to
  re-run — merges by task label/input id, so it won't clobber tasks you've
  added yourself. Works without the extension too: the template it installs
  lives at `tools/vera-vscode/tasks/vera-tasks.json`, plain copy-pasteable
  into any `.vscode/tasks.json`. Restart/stop require `VERA_DEV_MODE=1` on
  the server; the default `veraHost` input assumes `llm.int:8999` — edit it
  if yours differs.

- **Client mode** (`vera.clientMode` / command *Vera: Toggle client mode*) —
  registers this window with Vera as a controllable instance
  (`kind=vscode-client`) and long-polls for pushed actions: **open_file**,
  **run_command** (any VS Code command id), **terminal** (send text),
  **type_text**, **notify**, and **claude_task** — which runs `claude -p`
  *on this machine*, so it authenticates with whatever you're signed in as
  here (subscription login works with no API key). Vera's work queue treats
  a connected window like any other instance (`instance_id=any` will pick
  it), and `ide.remote.client.dispatch` pushes one-off actions. A status-bar
  item (📡 *Vera client*) shows it's live; click to disable.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `vera.baseUrl` | `http://127.0.0.1:8000` | Vera server HTTP/MCP endpoint |
| `vera.allow` | `ide.,fabric.,memory.,dream.,project.,web.` | cap-name prefixes exposed to Claude Code |
| `vera.askCapability` | `` | capability the *Ask Vera* box invokes with `{query}` |
| `vera.clientMode` | `false` | let Vera control this window (see above) |
| `vera.clientLabel` | `` | label in Vera's instance list (default hostname · folder) |
| `vera.clientToken` | `` | optional shared secret required on every poll/result |
| `vera.clientDefaultModel` | `opus` | fallback `--model` for client-mode Claude Code tasks that don't specify their own (Vera's enqueue box and `ide.remote.queue.add`/`ide.remote.run` both take a per-task `model`) — pins to a model your account definitely has; blank lets the local `claude` CLI's own default decide (which can be a paid/credit-gated model you don't have entitlement for, failing the task instead of falling back) |

## Requirements

- A reachable Vera server (its HTTP endpoint).
- `python3` on the machine running VS Code (the MCP bridge is a stdlib-only
  Python script).
- The `claude` CLI on PATH if you want automatic `claude mcp add` registration;
  otherwise the generated `.mcp.json` is enough for project-scoped Claude Code
  runs.

## Quick install (recommended)

Vera packages and serves this extension itself — you don't need Node or `vsce`.
On the machine with your desktop VS Code, run the one-liner from Vera's connect
page (`https://<vera>/vscode/connect`):

```powershell
# Windows (paste as-is; the branch below avoids a scriptblock cert callback,
# which throws "no Runspace available" on the TLS I/O thread in Windows
# PowerShell 5.1 and fails the handshake intermittently)
if($PSVersionTable.PSVersion.Major -ge 6){$s=irm https://<vera>/vscode/connect/connect.ps1 -SkipCertificateCheck}else{if(-not ([Management.Automation.PSTypeName]'VeraTrustAll').Type){Add-Type -TypeDefinition 'using System.Net;using System.Security.Cryptography.X509Certificates;public class VeraTrustAll : ICertificatePolicy { public bool CheckValidationResult(ServicePoint sp,X509Certificate c,WebRequest r,int p){return true;} }'};[Net.ServicePointManager]::CertificatePolicy=New-Object VeraTrustAll;[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;$s=irm https://<vera>/vscode/connect/connect.ps1};iex $s
```

```bash
# macOS / Linux
curl -fsSLk https://<vera>/vscode/connect/connect.sh | bash
```

It installs the extension, sets `vera.baseUrl` + `vera.clientMode`, and trusts
Vera's TLS cert (fixing the code-server webview "service worker SSL error").
After a reload the window shows up in Vera and can be sent Claude Code tasks.

## Build & install (manual)

```bash
cd tools/vera-vscode
npm install           # only dev dependency: @types/vscode
npx vsce package      # produces vera-vscode-0.1.0.vsix
```

Then in VS Code / code-server: **Extensions → … → Install from VSIX…** and pick
the generated `.vsix`. Set `vera.baseUrl` to your Vera server.

> Vera can also build the `.vsix` on demand — `ide.vscode.extension.build`
> (`mode="zip"` in-process, or `mode="vsce"` in a throwaway container) — and
> always serves a fresh one at `GET /vscode/connect/extension.vsix`.

> The MCP bridge served by `GET /ide/remote/bridge/source` is the exact same
> `vera/ide/vera_mcp_bridge.py` the server-side `ide.remote.bridge.install`
> deploys, so the editor path and the SSH path stay in sync.

## Troubleshooting

Installed but not showing up in your current profile, or installed but
won't connect? See `TROUBLESHOOTING.md` in this directory.
