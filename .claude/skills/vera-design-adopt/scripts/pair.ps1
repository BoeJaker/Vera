# Side-by-side PNG: design render on the left, live render on the right, a 24px gutter, labels on top.
#   pwsh scripts/pair.ps1 <design.png> <live.png> <out.png> [leftLabel] [rightLabel]
# Paths may be local or UNC (Notes/adopt-shots on the repo share): they are resolved with .NET, not the
# PowerShell provider, whose UNC form ("Microsoft.PowerShell.Core\FileSystem::\\host\...") System.Drawing cannot open.
param([Parameter(Mandatory)][string]$Left, [Parameter(Mandatory)][string]$Right, [Parameter(Mandatory)][string]$Out, [string]$LeftLabel = 'design', [string]$RightLabel = 'live')
Add-Type -AssemblyName System.Drawing
$full = { param($p) if ([IO.Path]::IsPathRooted($p)) { [IO.Path]::GetFullPath($p) } else { [IO.Path]::GetFullPath([IO.Path]::Combine((Get-Location).ProviderPath, $p)) } }
$lp = & $full $Left; $rp = & $full $Right; $op = & $full $Out
$outDir = [IO.Path]::GetDirectoryName($op); if ($outDir -and -not [IO.Directory]::Exists($outDir)) { [IO.Directory]::CreateDirectory($outDir) | Out-Null }
$a = [System.Drawing.Image]::FromFile($lp); $b = [System.Drawing.Image]::FromFile($rp)
$h = [Math]::Max($a.Height, $b.Height) + 28; $w = $a.Width + $b.Width + 24
$bmp = New-Object System.Drawing.Bitmap($w, $h); $g = [System.Drawing.Graphics]::FromImage($bmp)
$g.Clear([System.Drawing.Color]::FromArgb(14, 15, 18))
$font = New-Object System.Drawing.Font('Segoe UI', 11); $br = [System.Drawing.Brushes]::Gainsboro
$g.DrawString($LeftLabel, $font, $br, 4, 4); $g.DrawString($RightLabel, $font, $br, ($a.Width + 28), 4)
$g.DrawImage($a, 0, 28, $a.Width, $a.Height); $g.DrawImage($b, ($a.Width + 24), 28, $b.Width, $b.Height)
$bmp.Save($op, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose(); $a.Dispose(); $b.Dispose()
Write-Output ("pair → " + $op + " (" + $w + "x" + $h + ")")
