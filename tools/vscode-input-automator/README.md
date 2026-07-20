# Input Automator (VS Code extension)

A panel with a text box that automates input inside VS Code. Type or compose
text in the panel, pick a target, and the extension sends it for you — with
optional delays and repeats, so it works as a lightweight input macro tool.

## Targets

| Target | What it does |
|---|---|
| Editor — simulate typing | Replays the text through the real keyboard input pipeline (`type` command), so autocomplete, auto-closing brackets and snippets fire exactly as if you typed it. Supports a per-character delay for realistic pacing. |
| Editor — insert instantly | Inserts the whole text at every cursor/selection in one edit. |
| Terminal | Sends the text to the active integrated terminal (creates one if none), optionally pressing Enter. |
| VS Code command | Runs any command id, with optional JSON args (an array is spread into multiple arguments). |

## Automation options

- **Start delay** — countdown before the first send, giving you time to focus the right editor/terminal.
- **Per-char delay** — typing speed for the simulate-typing target.
- **Repeat / Interval** — send N times with a pause between runs.
- **Snippets** — save the whole form (text + target + timings) under a name and re-run it with one click. Stored globally, shared across windows.
- **Stop** — cancels a running job at any point.

## Where it lives

- **Tab**: run the command `Input Automator: Open as Tab` (default keybinding `Ctrl+Alt+I`) to open it as an editor tab beside your file.
- **Panel**: an "Input Automator" view is also contributed to the bottom panel area (next to Terminal/Output) — right-click the panel tab bar to show it if hidden.

Editor targets always go to the **last focused text editor**, so clicking into
the automator panel doesn't lose your target.

## Run it (development)

Open this folder in VS Code and press **F5** — a new Extension Development
Host window opens with the extension loaded (launch config included).

## Install it permanently

```sh
cd tools/vscode-input-automator
npx @vscode/vsce package          # produces input-automator-0.1.0.vsix
code --install-extension input-automator-0.1.0.vsix
```

(No build step — the extension is plain JavaScript with zero dependencies.)
