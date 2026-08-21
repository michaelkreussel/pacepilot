<#
.SYNOPSIS
    Create a reusable agent-browser auth state for local PacePilot development.

.DESCRIPTION
    Opens real Chrome with a dedicated automation profile and a Chrome DevTools
    Protocol (CDP) debugging port, waits until a manual Google sign-in has set
    the PacePilot session cookie, then saves the browser auth state so
    agent-browser can reuse it with --state.

.PARAMETER Action
    What to do:
      browser  Open (or reuse) the automation Chrome window.
      wait     Wait until sign-in completes (session cookie present).
      save     Save the current auth state with agent-browser.
      get      browser + wait + save in one run (default).
      check    Report whether the automation browser is running and signed in.

.PARAMETER Port
    CDP debugging port (default 9222).

.PARAMETER AppUrl
    Local PacePilot origin (default http://127.0.0.1:8000/).

.PARAMETER Profile
    Dedicated Chrome profile directory. Defaults to %USERPROFILE%\.pacepilot-browser.

.PARAMETER StateFile
    Where to save agent-browser auth state (default pacepilot-auth.json).

.PARAMETER TimeoutSeconds
    How long to wait for sign-in (default 600).

.EXAMPLE
    .\scripts\agent_browser_session.ps1 -Action get

.EXAMPLE
    .\scripts\agent_browser_session.ps1 -Action wait -TimeoutSeconds 300
#>

[CmdletBinding()]
param(
    [ValidateSet("browser", "wait", "save", "get", "check")]
    [string]$Action = "get",

    [int]$Port = 9222,
    [string]$AppUrl = "http://127.0.0.1:8000/",
    [string]$Profile = "",
    [string]$StateFile = "pacepilot-auth.json",
    [int]$TimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"
$SessionCookieName = "pacepilot_session"
$PollSeconds = 3

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message"
}

function Test-Cdp {
    try {
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 2
        return $true
    } catch {
        return $false
    }
}

function Find-Chrome {
    if ($env:CHROME_PATH -and (Test-Path -LiteralPath $env:CHROME_PATH)) {
        return $env:CHROME_PATH
    }
    $candidates = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
    )
    return $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}

function Resolve-Profile {
    if ($Profile) {
        return $Profile
    }
    $homeDir = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
    return Join-Path $homeDir ".pacepilot-browser"
}

function Get-AgentBrowser {
    $exe = Get-Command agent-browser -ErrorAction SilentlyContinue
    if (-not $exe) {
        throw "agent-browser is not installed. Install it with: npm i -g agent-browser && agent-browser install"
    }
    return $exe.Source
}

function Invoke-AgentBrowser {
    param([string[]]$Arguments)
    $exe = Get-AgentBrowser
    $output = & $exe @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "agent-browser $($Arguments -join ' ') failed (exit $LASTEXITCODE)."
    }
    return ($output | Out-String)
}

function Open-Browser {
    if (Test-Cdp) {
        Write-Step "Chrome is already running with CDP on port $Port; reusing it."
        return
    }
    $chrome = Find-Chrome
    if (-not $chrome) {
        throw "Chrome not found. Install Chrome or point CHROME_PATH at chrome.exe."
    }
    $profileDir = Resolve-Profile
    if (-not (Test-Path -LiteralPath $profileDir)) {
        New-Item -ItemType Directory -Path $profileDir -Force | Out-Null
    }
    $arguments = @(
        "--user-data-dir=$profileDir",
        "--remote-debugging-port=$Port",
        $AppUrl
    )
    Start-Process -FilePath $chrome -ArgumentList $arguments | Out-Null
    Write-Step "Launched Chrome ($chrome) with profile $profileDir and CDP port $Port."
    Write-Step "Sign in with Google in the new window."

    $deadline = (Get-Date).AddSeconds(20)
    while (-not (Test-Cdp) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-Cdp)) {
        throw "Chrome did not open CDP port $Port. If Chrome is already running with this profile (started without --remote-debugging-port), close it and run again."
    }
}

function Wait-ForSignIn {
    Write-Step "Waiting for sign-in on $AppUrl (up to $TimeoutSeconds seconds)."
    Write-Step "Complete the Google sign-in in the Chrome window; waiting for the '$SessionCookieName' cookie..."

    $cdpDeadline = (Get-Date).AddSeconds(15)
    while (-not (Test-Cdp) -and (Get-Date) -lt $cdpDeadline) {
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-Cdp)) {
        throw "No browser found on CDP port $Port. Run 'just open-browser' first."
    }

    $elapsed = 0
    $lastNotice = 0
    while ($elapsed -lt $TimeoutSeconds) {
        try {
            $output = Invoke-AgentBrowser @("--cdp", "$Port", "cookies")
            if ($output -match [regex]::Escape($SessionCookieName)) {
                Write-Step "Signed in: '$SessionCookieName' cookie found."
                return
            }
        } catch {
            # daemon may still be connecting to CDP; ignore and retry
        }
        $elapsed += $PollSeconds
        if ($elapsed - $lastNotice -ge 30) {
            $lastNotice = $elapsed
            Write-Host "    ... still waiting (${elapsed}s)."
        }
        Start-Sleep -Seconds $PollSeconds
    }
    throw "Timed out after $TimeoutSeconds seconds waiting for sign-in. Restart with 'just get-session'."
}

function Resolve-StateFile {
    if ([System.IO.Path]::IsPathRooted($StateFile)) {
        return $StateFile
    }
    return Join-Path (Get-Location).Path $StateFile
}

function Save-State {
    $target = Resolve-StateFile
    Write-Step "Saving auth state to $target ..."
    Invoke-AgentBrowser @("--cdp", "$Port", "state", "save", $target)
    if (-not (Test-Path -LiteralPath $target)) {
        throw "agent-browser did not create $target."
    }
    Write-Step "Saved $target."
}

function Show-Status {
    if (-not (Test-Cdp)) {
        Write-Host "Automation Chrome: not running on CDP port $Port (run 'just open-browser')."
        return
    }
    Write-Host "Automation Chrome: running on CDP port $Port."
    try {
        $output = Invoke-AgentBrowser @("--cdp", "$Port", "cookies")
        if ($output -match [regex]::Escape($SessionCookieName)) {
            Write-Host "Sign-in state:     signed in ('$SessionCookieName' cookie present)."
        } else {
            Write-Host "Sign-in state:     not signed in (run 'just wait-login' after signing in)."
        }
    } catch {
        Write-Host "Sign-in state:     unknown ($($_.Exception.Message))."
    }
}

function Get-Session {
    Open-Browser
    Wait-ForSignIn
    Save-State
    $target = Resolve-StateFile
    Write-Host ""
    Write-Host "Auth state saved to $target. Reuse it with:"
    Write-Host "  agent-browser --state $target open $AppUrl"
    Write-Host ""
    Write-Host "Or attach to the still-running Chrome window with:"
    Write-Host "  agent-browser --cdp $Port open $AppUrl"
}

try {
    if ($Action -ne "browser") {
        $null = Get-AgentBrowser
    }
    switch ($Action) {
        "browser" { Open-Browser }
        "wait" { Wait-ForSignIn }
        "save" { Save-State }
        "get" { Get-Session }
        "check" { Show-Status }
    }
} catch {
    Write-Host ""
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
