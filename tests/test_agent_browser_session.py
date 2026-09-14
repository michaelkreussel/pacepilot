"""Exercise the Windows helper's login decisions without launching Chrome or using cookies."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "agent_browser_session.ps1"
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")


@pytest.mark.parametrize(
    ("status", "origin", "path", "expected"),
    [
        (200, "http://localhost:8000", "/settings", "authenticated"),
        (200, "http://localhost:8000", "/onboarding", "authenticated"),
        (200, "http://localhost:8000", "/login", "unauthenticated"),
        (200, "http://elsewhere.invalid", "/settings", "unavailable"),
        (500, "http://localhost:8000", "/settings", "unavailable"),
    ],
)
def test_login_requires_protected_page(status: int, origin: str, path: str, expected: str) -> None:
    result = {"status": status, "origin": origin, "path": path}
    powershell = f"""
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{str(SCRIPT).replace("'", "''")}', [ref]$null, [ref]$null)
$functions = $ast.FindAll({{param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}}, $false)
. ([scriptblock]::Create(($functions.Extent.Text -join "`n")))
$AppUrl = 'http://localhost:8000/'
function Invoke-AgentBrowser {{
    param([string[]]$Arguments)
    if ($Arguments.Count -ne 3 -or $Arguments[0] -ne 'eval' -or $Arguments[1] -ne '-b') {{
        throw 'Unexpected browser operation or missing arguments'
    }}
    return @{{ data = @{{ result = ('{json.dumps(result)}' | ConvertFrom-Json) }} }}
}}
Get-LoginState
"""
    assert POWERSHELL is not None
    completed = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", powershell],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == expected


def test_browser_reply_does_not_wait_for_stdout_to_close(tmp_path: Path) -> None:
    fake_cli = tmp_path / "fake browser.ps1"
    fake_cli.write_text(
        "@{success=$true;data=@{arguments=@($args)}} | ConvertTo-Json -Compress\n"
        "Start-Sleep -Seconds 15\n",
        encoding="utf-8",
    )
    powershell = f"""
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{str(SCRIPT).replace("'", "''")}', [ref]$null, [ref]$null)
$functions = $ast.FindAll({{param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}}, $false)
. ([scriptblock]::Create(($functions.Extent.Text -join "`n")))
$BrowserSession = 'test-session'
$Port = 9222
function Get-Command {{
    return @{{Source='{str(fake_cli).replace("'", "''")}'}}
}}
$result = Invoke-AgentBrowser -Arguments @('eval', 'text with spaces and ''quotes''')
$result.data.arguments | ConvertTo-Json -Compress
"""
    assert POWERSHELL is not None
    # Windows descendants inherit the test runner's pipe handles as well. Use a
    # file so subprocess waits for the caller, not for the simulated daemon's EOF.
    output_path = tmp_path / "result.txt"
    with output_path.open("w", encoding="utf-8") as output:
        completed = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", powershell],
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
    result = output_path.read_text(encoding="utf-8")
    assert completed.returncode == 0, result
    assert json.loads(result) == [
        "--session",
        "test-session",
        "--cdp",
        "9222",
        "--pin-tab",
        "--json",
        "eval",
        "text with spaces and 'quotes'",
    ]
