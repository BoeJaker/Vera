# Render a live Vera page (a sandbox or prod URL) with the same headless probe used for design boards,
# so a design render and a live render are comparable one to one.
#   pwsh scripts/shot-live.ps1 <url> <out dir> <out.png> [waitMs] [cdpOffset]
# Optional env before calling: CLICK='Explode' (click a button by text), CLICKXY='x,y', WHEEL='{"x":900,"y":500,"dy":120,"n":4}'
param([Parameter(Mandatory)][string]$Url, [Parameter(Mandatory)][string]$OutDir, [Parameter(Mandatory)][string]$Out, [int]$WaitMs = 12000, [int]$CdpOffset = 500)
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$env:CLIP = $null; $env:CDP_OFFSET = [string]$CdpOffset
$probe = Join-Path $PSScriptRoot 'probe.mjs'
node $probe $OutDir $Url $WaitMs $Out
