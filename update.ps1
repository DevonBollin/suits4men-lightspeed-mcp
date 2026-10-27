<#
Update this computer's copy of the Suits 4 Men Lightspeed server to the newest released version.

In PowerShell, in the project folder:

    powershell -ExecutionPolicy Bypass -File .\update.ps1

(-ExecutionPolicy Bypass lets Windows run this one script; it changes no setting.)

Meant for the store's computer, which only runs released versions: the "stable" branch
on GitHub. The developer's computer works on "main" and releases by pushing to "stable".

What it does:
  1. Refuses to run if files tracked here were edited on this computer.
  2. Fetches "stable" from GitHub and moves to it (fast-forward only, never a merge).
  3. Installs any new or changed packages into .venv (uv sync).
  4. Runs the tests. If any fail, goes back to the version it started from and says so.
  5. Tells you to restart Claude Desktop, which loads the new version.

It never touches state\ (the write switch and the write log), backups\, or the saved
Lightspeed login (Windows Credential Manager).
#>

param([string]$Branch = "stable")

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Fail([string]$Message) {
    Write-Host ""
    Write-Host $Message -ForegroundColor Red
    exit 1
}

function Run([string]$What) {
    # Native programs don't stop the script on failure in Windows PowerShell; check their exit code.
    if ($LASTEXITCODE -ne 0) { Fail "Stopped: $What failed (exit code $LASTEXITCODE)." }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "Git is not installed (see README, 'Setting up another computer')." }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail "uv is not installed (see README, 'Setting up another computer')." }

# 1. This copy only ever runs released versions, so nothing may be edited here.
$edited = git status --porcelain --untracked-files=no
Run "git status"
if ($edited) {
    Write-Host $edited
    Fail "Stopped: the files above were changed on this computer. Make changes on the developer's computer instead."
}

# 2. Fetch and move to the newest release.
git fetch origin
Run "git fetch"
$current = git rev-parse --abbrev-ref HEAD
if ($current -ne $Branch) {
    git show-ref --verify --quiet "refs/heads/$Branch"
    if ($LASTEXITCODE -eq 0) { git checkout $Branch } else { git checkout -b $Branch --track "origin/$Branch" }
    Run "switching to $Branch"
}
$before = git rev-parse HEAD
git merge --ff-only "origin/$Branch"
Run "moving to the newest $Branch"
$after = git rev-parse HEAD

if ($before -eq $after) {
    Write-Host ""
    Write-Host "Already up to date ($Branch @ $($after.Substring(0, 7)))." -ForegroundColor Green
    exit 0
}

Write-Host ""
Write-Host "New in this version:"
git log --oneline "$before..$after"

# 3. Packages, into .venv only.
uv sync
Run "uv sync"

# 4. Tests. A version that fails them doesn't stay.
.venv\Scripts\python.exe -m pytest -q
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "The tests failed, so this computer goes back to $($before.Substring(0, 7))." -ForegroundColor Red
    # Safe here: step 1 made sure nothing tracked was edited on this computer.
    git reset --hard $before
    uv sync
    Fail "Nothing was updated. Tell the developer which tests failed (shown above)."
}

# 5. Done.
Write-Host ""
Write-Host "Updated $($before.Substring(0, 7)) -> $($after.Substring(0, 7))." -ForegroundColor Green
Write-Host "Now quit Claude Desktop (right-click its tray icon, Quit) and start it again to load this version."
