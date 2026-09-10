# Read a published design canvas back into working files.
#   pwsh scripts/extract.ps1 <saved artifact html> <fresh empty dir> [design skill base dir]
# The design skill's helper (seed-canvas.mjs) does the work; this wrapper finds it and refuses a non-empty target.
param([Parameter(Mandatory)][string]$Html, [Parameter(Mandatory)][string]$To, [string]$SkillBase = '')
if (-not $SkillBase) {
  $cand = Get-ChildItem "$env:LOCALAPPDATA\Temp\claude\bundled-skills" -Recurse -Filter 'seed-canvas.mjs' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not $cand) { throw 'design skill helper (seed-canvas.mjs) not found — run /design once so it is extracted, or pass -SkillBase' }
  $SkillBase = $cand.DirectoryName
}
if ((Test-Path $To) -and (Get-ChildItem $To | Measure-Object).Count -gt 0) { throw "target $To is not empty — extract into a FRESH directory" }
New-Item -ItemType Directory -Force -Path $To | Out-Null
node "$SkillBase\seed-canvas.mjs" --extract $Html --to $To
Get-ChildItem $To | ForEach-Object { $_.Name }
