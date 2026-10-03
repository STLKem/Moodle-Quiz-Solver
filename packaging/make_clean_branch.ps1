# Creates orphan branch github-clean with current tree (no secret history).
# Does NOT push. Does NOT modify main.

$ErrorActionPreference = "Stop"
$git = "C:\Program Files\Git\bin\git.exe"
$tmp = Join-Path $env:TEMP "git-empty-config"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
Set-Content -Path (Join-Path $tmp ".gitconfig") -Value "" -Encoding ascii
$env:HOME = $tmp
$env:GIT_CONFIG_GLOBAL = (Join-Path $tmp ".gitconfig")
$env:GIT_CONFIG_SYSTEM = (Join-Path $tmp ".gitconfig")

Set-Location "D:\Moodle_Solver"

function G {
  & $git -c safe.directory=* -c user.name=Kem -c user.email=kem@users.noreply.github.com @args
  if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') failed: $LASTEXITCODE" }
}

# Ensure we are not mid-merge
G checkout main

# Drop previous attempt if any
cmd /c "`"$git`" -c safe.directory=* branch -D github-clean >nul 2>nul"

G checkout --orphan github-clean
G rm -rf --cached .
G add -A

$bad = G diff --cached --name-only | Select-String -Pattern '(^|/)config\.yaml$|configg\.yaml|(^|/)Release/|(^|/)\.env$'
if ($bad) {
  Write-Host "REFUSING: secret-like paths staged:"
  $bad
  exit 1
}

$msgFile = Join-Path $env:TEMP "moodle-commit-msg.txt"
Set-Content -Path $msgFile -Value "Publish clean Moodle Solver tree without local secrets." -Encoding utf8
G commit -F $msgFile

Write-Host "OK branch github-clean created"
G log -1 --oneline
G ls-files | Select-String -Pattern 'config\.yaml$|configg|Release/'
Write-Host "staged/tracked secret paths above should be empty"
G status -sb
