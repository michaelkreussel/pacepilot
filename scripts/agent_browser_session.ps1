<#
.SYNOPSIS
    Open dedicated Chrome for manual PacePilot login, then use Agent Browser via CDP.
.DESCRIPTION
    Uses localhost and a persistent separate Chrome profile. A protected page verifies
    authentication; cookies are never inspected. The save action is a legacy export.
#>
[CmdletBinding()]
param(
    [ValidateSet("browser", "wait", "save", "get", "check")]
    [string]$Action = "get",
    [ValidateRange(1, 65535)]
    [int]$Port = 9222,
    [string]$AppUrl = "http://localhost:8000/",
    [string]$Profile = "",
    [string]$StateFile = "pacepilot-auth.json",
    [ValidateRange(1, 86400)]
    [int]$TimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"
$BrowserSession = "pacepilot-helper-$Port"
$ProbeOpen = $false

function Get-AppUri {
    $uri = $null
    if (-not [Uri]::TryCreate($AppUrl, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -ne "http" -or $uri.Host -ne "localhost" -or $uri.Port -ne 8000 -or
        $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -ne "/") {
        throw "Verwende fuer PacePilot ausschliesslich http://localhost:8000/."
    }
    return $uri
}

function Test-AppAvailable {
    try {
        $health = Invoke-RestMethod -Uri "$($AppUrl.TrimEnd('/'))/api/health" -TimeoutSec 5
        return $health.status -eq "ok"
    } catch { return $false }
}

function Assert-AppAvailable {
    if (-not (Test-AppAvailable)) {
        throw "PacePilot ist nicht erreichbar. Starte: uv run uvicorn app.main:app --host localhost --port 8000 --reload"
    }
}

function Resolve-Profile {
    $selected = if ($Profile) { $Profile } else { Join-Path $env:USERPROFILE ".pacepilot-browser" }
    $resolved = [IO.Path]::GetFullPath($selected).TrimEnd('\')
    $personal = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Google\Chrome\User Data"))
    if ($resolved -eq $personal -or $resolved.StartsWith("$personal\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Das persoenliche Chrome-Profil darf nicht verwendet werden. Nutze ein separates PacePilot-Profil."
    }
    return $resolved
}

function Get-ProfileChrome {
    $resolved = Resolve-Profile
    @(Get-CimInstance Win32_Process -Filter "name = 'chrome.exe'" | Where-Object {
        $command = $_.CommandLine
        if (-not $command -or $command -match '--type=') { return $false }
        $match = [regex]::Match($command, '--user-data-dir(?:=|\s+)(?:"([^"]+)"|(\S+))')
        if (-not $match.Success) { return $false }
        $directory = if ($match.Groups[1].Success) { $match.Groups[1].Value } else { $match.Groups[2].Value }
        [IO.Path]::GetFullPath($directory).TrimEnd('\') -eq $resolved
    })
}

function Test-Cdp {
    try {
        $null = Invoke-RestMethod -Uri "http://localhost:$Port/json/version" -TimeoutSec 5
        return $true
    } catch { return $false }
}

function Assert-Cdp {
    if (-not (Test-Cdp)) { throw "Chrome/CDP ist nicht erreichbar. Fuehre just agent-browser aus." }
    $matching = @(Get-ProfileChrome | Where-Object {
        $_.CommandLine -match "--remote-debugging-port(?:=|\s+)$Port(?:\s|$)"
    })
    if (-not $matching) {
        throw "CDP gehoert nicht zum dedizierten PacePilot-Profil. Verwende dessen Chrome-Fenster auf Port $Port."
    }
}

function Open-Browser {
    if (Test-Cdp) {
        Assert-Cdp
        Write-Host "Chrome/CDP ist bereit; das dedizierte Profil wird weiterverwendet."
        return
    }
    if (@(Get-ProfileChrome).Count) {
        throw "Das PacePilot-Browserprofil ist bereits ohne erreichbares Remote Debugging geoeffnet. Schliesse dieses dedizierte Chrome-Fenster und starte den Befehl erneut."
    }
    $chrome = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $chrome) { throw "Installiertes Google Chrome wurde nicht gefunden." }
    $directory = Resolve-Profile
    $null = New-Item -ItemType Directory -Path $directory -Force
    # Numeric loopback is CDP transport only, never the PacePilot origin.
    $launchArguments = @(
        "--user-data-dir=`"$directory`"", "--remote-debugging-port=$Port",
        "--remote-debugging-address=127.0.0.1", "--no-first-run", $AppUrl
    )
    Start-Process -FilePath $chrome -ArgumentList $launchArguments -WindowStyle Normal | Out-Null
    $deadline = (Get-Date).AddSeconds(15)
    while (-not (Test-Cdp) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    Assert-Cdp
    Write-Host "Dediziertes Chrome-Profil gestartet. Melde dich auf localhost:8000 manuell bei PacePilot an."
}

function Invoke-AgentBrowser {
    param([string[]]$Arguments)
    $exe = Get-Command agent-browser -ErrorAction SilentlyContinue
    if (-not $exe) { throw "agent-browser fehlt. Installiere es mit npm install -g agent-browser." }
    # On Windows a new daemon inherits stdout. Reading until EOF can therefore hang.
    # Read the CLI's single JSON response instead; never echo raw browser errors.
    $cliArguments = @("--session", $BrowserSession, "--cdp", "$Port", "--pin-tab", "--json") + $Arguments
    $quotedArguments = ($cliArguments | ForEach-Object { "'" + $_.Replace("'", "''") + "'" }) -join " "
    $command = "& '" + $exe.Source.Replace("'", "''") + "' " + $quotedArguments
    $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    $start = [Diagnostics.ProcessStartInfo]::new("powershell.exe", "-NoProfile -NonInteractive -EncodedCommand $encodedCommand")
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = $null
    try {
        $process = [Diagnostics.Process]::Start($start)
        $line = $process.StandardOutput.ReadLineAsync()
        if (-not $line.Wait(45000)) { throw "Timeout" }
        $result = $line.Result | ConvertFrom-Json
    } catch { throw "Agent Browser antwortet nicht korrekt. Chrome offen lassen und den Befehl erneut starten." }
    finally {
        if ($process) {
            # Only release our reader; never terminate Chrome or its CDP session.
            $process.StandardOutput.Dispose()
            $process.StandardError.Dispose()
            $process.Dispose()
        }
    }
    if (-not $result.success) { throw "Agent Browser konnte den Prueftab nicht lesen." }
    return $result
}

function Open-Probe {
    $url = "$($AppUrl.TrimEnd('/'))/api/health"
    $null = Invoke-AgentBrowser -Arguments @("tab", "new", $url)
    $script:ProbeOpen = $true
    $null = Invoke-AgentBrowser -Arguments @("wait", "--url", $url)
}

function Get-LoginState {
    # A separate same-origin tab leaves manual Google login untouched.
    # Read only status and final origin/path of an existing protected route.
    $javascript = @"
(async () => {
  try {
    if (location.origin !== 'http://localhost:8000') return {status: 0, origin: '', path: ''};
    const response = await fetch('/settings', {credentials: 'same-origin', redirect: 'follow'});
    const url = new URL(response.url);
    return {status: response.status, origin: url.origin, path: url.pathname};
  } catch { return {status: 0, origin: '', path: ''}; }
})()
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($javascript))
    $response = (Invoke-AgentBrowser -Arguments @("eval", "-b", $encoded)).data.result
    if ($response.origin -ne $AppUrl.TrimEnd('/') -or $response.status -ne 200) { return "unavailable" }
    if ($response.path -eq "/login") { return "unauthenticated" }
    if ($response.path -in @("/settings", "/onboarding")) { return "authenticated" }
    return "unavailable"
}

function Wait-ForSignIn {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    Write-Host "Warte auf die manuelle PacePilot-Anmeldung. Keine Chrome-Synchronisierung erforderlich."
    Write-Host "Wechsle dazu zum PacePilot-Tab; der separate /api/health-Tab dient nur der Pruefung."
    do {
        Assert-Cdp
        Assert-AppAvailable
        $state = Get-LoginState
        if ($state -eq "authenticated") {
            Write-Host "Erfolgreich angemeldet: geschuetzte PacePilot-Seite ist erreichbar."
            return
        }
        if ($state -eq "unavailable") { throw "PacePilot-Pruefseite ist nicht erreichbar oder meldet einen Fehler." }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
    throw "Noch nicht angemeldet. Google-Login im dedizierten Chrome abschliessen; danach just wait-login."
}
function Save-State {
    if ((Get-LoginState) -ne "authenticated") { throw "Kein Export: Benutzer ist nicht angemeldet." }
    $target = [IO.Path]::GetFullPath($StateFile)
    $null = Invoke-AgentBrowser -Arguments @("state", "save", $target)
    Write-Host "Legacy-Export gespeichert. Die Datei enthaelt lokale Auth-Daten und darf nicht committed werden."
}

try {
    $null = Get-AppUri
    Assert-AppAvailable
    if ($Action -in @("browser", "get")) { Open-Browser } else { Assert-Cdp }
    if ($Action -ne "browser") {
        Open-Probe
        switch ($Action) {
            "get" { Wait-ForSignIn }
            "wait" { Wait-ForSignIn }
            "save" { Save-State }
            "check" {
                switch (Get-LoginState) {
                    "authenticated" { Write-Host "Benutzer erfolgreich angemeldet; Chrome/CDP und PacePilot erreichbar." }
                    "unauthenticated" { throw "Benutzer nicht eingeloggt. Google-Login im PacePilot-Chrome abschliessen." }
                    default { throw "PacePilot-Pruefseite nicht erreichbar oder fehlerhaft." }
                }
            }
        }
    }
    if ($Action -in @("get", "wait")) {
        Write-Host "Session bleibt im Chrome-Profil. Kein Auth-State-Export erforderlich."
        Write-Host "agent-browser --session pacepilot --cdp $Port --pin-tab tab new http://localhost:8000/"
    }
} catch {
    Write-Host "FEHLER: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    if ($ProbeOpen) {
        try { $null = Invoke-AgentBrowser -Arguments @("tab", "close") } catch { }
    }
}
