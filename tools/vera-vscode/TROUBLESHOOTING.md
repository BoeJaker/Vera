# Vera for VS Code — troubleshooting

Field notes from real installs going wrong, kept next to the extension so
they don't get lost in chat history. See `README.md` for normal
install/config; this file is specifically "it's installed but I can't see
it" / "it won't connect."

## "I installed it but it's not in this profile" (VS Code Profiles)

VS Code Profiles each have their own extension set. `code
--install-extension <path>` run from a plain terminal — including from
inside a `Vera: Connect` one-liner or a manual `.vsix` install — **always
installs into the Default profile**, regardless of which profile's window
you ran the terminal from. The `code` CLI is stateless with respect to
that; there's no way for it to infer "the profile I'm currently looking
at" from a bare terminal invocation.

Symptom: the extension shows up fine in the Default profile, but is
completely absent in another profile — not listed as installed, not
listed as disabled, no commands under `Ctrl+Shift+P`, nothing in the
sidebar.

**Fix** — reinstall with the profile named explicitly:

```
code --install-extension <path-to-vera-vscode.vsix> --profile "<Exact Profile Name>"
```

Get the exact profile name (case-sensitive) from the Profile icon
(bottom-left of the Activity Bar) or `Ctrl+Shift+P` → **Profiles: Switch
Profile**.

If you don't have the `.vsix` file handy, get a fresh one from Vera's own
`/vscode/connect` page — **opened from inside the target-profile window**
— and use its one-liner/download. Check the generated command before
running it: as of this writing it does **not** append `--profile`, so
append it yourself to whatever `code --install-extension ...` command it
gives you.

One more thing worth checking if `code` still doesn't behave as expected:
confirm the `code` on your PATH is actually the same VS Code app you have
open (`code -v` in a terminal vs. Help → About in the GUI). A second VS
Code install (Insiders, a portable copy) shadowing PATH produces this
exact symptom too, independent of profiles.

## "It's installed and enabled but won't connect"

**Check `vera.baseUrl` first.** The extension's built-in default is
`http://127.0.0.1:8000` (see `README.md`'s settings table) — if this
setting was never actually written for the profile/workspace you're in
(easy to miss: settings are also profile-scoped, so a profile that needed
the extension re-added per the section above almost certainly also lacks
this setting), the extension silently tries to reach `127.0.0.1:8000`
and fails, with no obvious hint that it's the wrong host at all.

Open Settings (`Ctrl+,`, search `vera`) **in the profile you're actually
using** and set:

- `vera.baseUrl` → your real Vera server, e.g. `https://llm.int:8999`
- `vera.clientMode` → `true`, if you want this window live-controllable
  (this is what makes it register as a `vscode-client` instance Vera can
  see and dispatch tasks to)
- `vera.clientToken` → only needed if one was set when this instance was
  originally registered

**If `baseUrl` is already correct and it's still failing**, it's most
likely the TLS cert-pinning path: Vera serves a self-signed cert, and the
extension TOFU-pins it on first successful connect (see the "VS Code's
bundled Node doesn't consult the OS cert store" note below) — a since-
rotated cert or a bad first pin leaves it stuck. Run **"Vera: Forget
Certificate"** from the Command Palette, then reload the window to force
a fresh pin.

**If it still fails after both of those**, the extension's own status
panel shows the real underlying error text (`client.lastErr`, rendered
in the Vera sidebar webview) — that's the thing to read next, not this
doc. Common shapes seen so far, all already handled by the extension
itself but worth knowing about if something looks like it regressed:

- **PowerShell scriptblock cert-bypass is flaky, not just insecure.**
  `[Net.ServicePointManager]::ServerCertificateValidationCallback={$true}`
  sets a PS scriptblock as the callback; .NET can invoke it on a TLS I/O
  thread with no runspace attached, throwing `There is no Runspace
  available to run scripts in this thread` — surfaces as "The underlying
  connection was closed: An unexpected error occurred on a send."
  Reproduced via real Windows PowerShell 5.1 specifically (pwsh 7's
  `HttpWebRequest` is a different, non-representative shim). This is why
  `README.md`'s quick-install one-liner branches on PS version — a
  compiled `ICertificatePolicy` type for PS5.1, `-SkipCertificateCheck`
  for PS6+.
- **VS Code's bundled Node doesn't consult the OS certificate store.**
  Importing Vera's cert into `Cert:\CurrentUser\Root` fixes browsers and
  .NET but NOT the extension's own `https.request()` calls — Node gives
  `self signed certificate; ... try running Node.js with --use-system-ca`,
  a flag that can't be passed to Electron's bundled Node. The extension
  works around this itself by fetching `/vscode/connect/cert` once
  (insecurely) and caching it to `ctx.globalStorageUri`, passing it as
  `ca` on every request thereafter — "Vera: Forget Certificate" clears
  that cache for a fresh pin.
- **Self-signed cert SAN list can miss the LAN IP**, if Vera is reachable
  by an IP/hostname its own cert-generation logic didn't know to include
  (`TLS_EXTRA_SANS` on the server side is the fix for that half — not
  something the extension can work around on its own).

## Where this came from

These are real incidents hit and fixed live during a single support
session (2026-08-03), not hypothetical. Add to this file rather than
letting the next occurrence get re-diagnosed from scratch.
