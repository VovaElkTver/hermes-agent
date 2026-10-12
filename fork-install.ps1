# ============================================================================
# Hermes Agent (VovaElkTver fork) one-line installer for Windows.
#
#   iwr -useb https://raw.githubusercontent.com/VovaElkTver/hermes-agent/main/fork-install.ps1 | iex
#
# Clones the FORK into $env:LOCALAPPDATA\hermes\hermes-agent (or $env:HERMES_HOME)
# and hands over to setup-hermes.ps1 for the real install. No setup logic here.
#
# Env overrides: HERMES_FORK_URL, HERMES_FORK_BRANCH, HERMES_HOME, DRY_RUN=1
# ============================================================================
$ErrorActionPreference = "Stop"
$ForkUrl  = if ($env:HERMES_FORK_URL)    { $env:HERMES_FORK_URL }    else { "https://github.com/VovaElkTver/hermes-agent" }
$Branch   = if ($env:HERMES_FORK_BRANCH) { $env:HERMES_FORK_BRANCH } else { "main" }
$Base     = if ($env:HERMES_HOME)        { $env:HERMES_HOME }        else { "$env:LOCALAPPDATA\hermes" }
$Dest     = Join-Path $Base "hermes-agent"
$DryRun   = $env:DRY_RUN -eq "1"

function Invoke-Step([string]$label, [scriptblock]$body) {
  if ($DryRun) { Write-Host "[dry-run] $label" -ForegroundColor Cyan; return }
  Write-Host $label -ForegroundColor Cyan; & $body
}

if (Test-Path (Join-Path $Dest ".git")) {
  $cur = (git -C $Dest remote get-url origin 2>$null)
  if ($cur -and $cur -ne $ForkUrl) {
    Write-Host "existing checkout origin is: $cur`nexpected:                    $ForkUrl`nrefusing to touch it — remove/move $Dest or set HERMES_FORK_URL." -ForegroundColor Red
    exit 1
  }
  Invoke-Step "Updating existing checkout ($Branch)" {
    git -C $Dest fetch -q origin $Branch
    git -C $Dest checkout -q $Branch
    git -C $Dest merge --ff-only -q "origin/$Branch"
    if ($LASTEXITCODE) { throw "update failed ($LASTEXITCODE)" }
  }
} else {
  Invoke-Step "Cloning $ForkUrl ($Branch) -> $Dest" {
    New-Item -ItemType Directory -Force -Path $Base | Out-Null
    git clone -q --branch $Branch $ForkUrl $Dest
    if ($LASTEXITCODE) { throw "clone failed ($LASTEXITCODE)" }
  }
}

$setup = Join-Path $Dest "setup-hermes.ps1"
if ($DryRun) { Write-Host "[dry-run] & $setup" -ForegroundColor Cyan }
else { & $setup; if ($LASTEXITCODE) { exit $LASTEXITCODE } }
Write-Host "Then: & '$Dest\venv\Scripts\Activate.ps1'; hermes gateway install" -ForegroundColor Cyan
