# Wire git to the version-controlled hooks in tools/hooks (Windows helper).
$ErrorActionPreference = "Stop"
$root = (git rev-parse --show-toplevel).Trim()
git -C $root config core.hooksPath tools/hooks
Write-Host "core.hooksPath -> tools/hooks. Secret scan now runs on every commit."
