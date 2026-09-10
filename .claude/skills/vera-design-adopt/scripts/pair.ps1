# Side-by-side PNG: design render on the left, live render on the right, a 24px gutter, labels on top.
#   pwsh scripts/pair.ps1 <design.png> <live.png> <out.png> [leftLabel] [rightLabel]
param([Parameter(Mandatory)][string]$Left, [Parameter(Mandatory)][string]$Right, [Parameter(Mandatory)][string]$Out, [string]$LeftLabel = 'design', [string]$RightLabel = 'live')
Add-Type -AssemblyName System.Drawing
$a = [System.Drawing.Image]::FromFile((Resolve-Path $Left)); $b = [System.Drawing.Image]::FromFile((Resolve-Path $Right))
$h = [Math]::Max($a.Height, $b.Height) + 28; $w = $a.Width + $b.Width + 24
$bmp = New-Object System.Drawing.Bitmap($w, $h); $g = [System.Drawing.Graphics]::FromImage($bmp)
$g.Clear([System.Drawing.Color]::FromArgb(14, 15, 18))
$font = New-Object System.Drawing.Font('Segoe UI', 11); $br = [System.Drawing.Brushes]::Gainsboro
$g.DrawString($LeftLabel, $font, $br, 4, 4); $g.DrawString($RightLabel, $font, $br, ($a.Width + 28), 4)
$g.DrawImage($a, 0, 28, $a.Width, $a.Height); $g.DrawImage($b, ($a.Width + 24), 28, $b.Width, $b.Height)
$outDir = Split-Path -Parent $Out; if ($outDir -and -not (Test-Path $outDir)) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }
$bmp.Save((Join-Path (Resolve-Path (if ($outDir) { $outDir } else { '.' })) (Split-Path -Leaf $Out)), [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose(); $a.Dispose(); $b.Dispose()
Write-Output ("pair → " + $Out)
