param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $SimulatorArgs
)

$ErrorActionPreference = "Stop"

chcp 65001 | Out-Null
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
$VenvActivate = Join-Path $ProjectRoot ".venv\Scripts\Activate.ps1"

if (-not (Test-Path -LiteralPath $VenvActivate)) {
    throw "未找到虚拟环境: $VenvActivate"
}

. $VenvActivate
$env:PYTHONPATH = [string]$ProjectRoot
Set-Location -LiteralPath $ScriptDir

python -m search_simulator --cli @SimulatorArgs
