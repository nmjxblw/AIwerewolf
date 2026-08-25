[CmdletBinding()]
param(
    [ValidateSet("fake", "real")]
    [string]$Mode = "fake",
    [switch]$SkipInstall,
    [switch]$NoBrowser,
    [switch]$CheckOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = $PSScriptRoot
$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
$rootEnvExample = Join-Path $repoRoot ".env.example"
$rootEnvFile = Join-Path $repoRoot ".env"
$frontendDir = Join-Path $repoRoot "frontend"
$frontendEnvExample = Join-Path $frontendDir ".env.example"
$frontendEnvLocal = Join-Path $frontendDir ".env.local"
$frontendModules = Join-Path $frontendDir "node_modules"
$backendUrl = "http://127.0.0.1:8001"
$frontendUrl = "http://localhost:3001"

function ConvertTo-PowerShellLiteral {
    param([Parameter(Mandatory = $true)][string]$Value)

    return "'" + $Value.Replace("'", "''") + "'"
}

function Read-DotEnvValues {
    param([Parameter(Mandatory = $true)][string]$Path)

    $values = @{}
    foreach ($rawLine in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            continue
        }
        $parts = $line.Split("=", 2)
        $key = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")
        if ($key) {
            $values[$key] = $value
        }
    }
    return $values
}

function Get-ConfiguredValue {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Values,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $processValue = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($processValue)) {
        return $processValue.Trim()
    }
    if ($Values.ContainsKey($Name)) {
        return [string]$Values[$Name]
    }
    return ""
}

function Test-UsableCredential {
    param([AllowEmptyString()][string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $false
    }
    return $Value -notmatch '^<.*>$|your[-_ ]|replace[-_ ]|^sk-xxx$'
}

function Open-RootEnvEditor {
    if ($CheckOnly) {
        return
    }
    try {
        Start-Process -FilePath "notepad.exe" -ArgumentList @($rootEnvFile) | Out-Null
    }
    catch {
        Write-Warning "Could not open Notepad automatically. Edit $rootEnvFile manually."
    }
}

function Assert-RealModeConfiguration {
    $values = Read-DotEnvValues -Path $rootEnvFile
    $provider = Get-ConfiguredValue -Values $values -Name "LLM_PROVIDER"
    $fakeProviders = @("fake", "fake_llm", "offline_llm")

    if ([string]::IsNullOrWhiteSpace($provider) -or $provider -in $fakeProviders) {
        Open-RootEnvEditor
        throw "Set LLM_PROVIDER to a real provider in $rootEnvFile, then run the launcher again."
    }

    # Windows PowerShell 5.1 unwraps a one-item switch result into a scalar.
    # The outer array expression keeps Count/foreach behavior stable for every provider.
    $credentialNames = @(switch ($provider.ToLowerInvariant()) {
        "doubao" { @("DOUBAO_API_KEY", "ARK_API_KEY", "ANTHROPIC_AUTH_TOKEN") }
        "deepseek" { @("DEEPSEEK_API_KEY") }
        "dsv4flash" { @("DSV4FLASH_API_KEY") }
        "ark" { @("DSV4FLASH_API_KEY", "ARK_API_KEY") }
        "weapi" { @("WEAPI_API_KEY") }
        "weapi_pw" { @("WEAPI_API_KEY") }
        "anthropic" { @("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY") }
        "mimo" { @() }
        "local_mimo" { @() }
        default {
            Open-RootEnvEditor
            throw "Unsupported LLM_PROVIDER '$provider' in $rootEnvFile."
        }
    })

    if ($credentialNames.Count -gt 0) {
        $hasCredential = $false
        foreach ($credentialName in $credentialNames) {
            $credential = Get-ConfiguredValue -Values $values -Name $credentialName
            if (Test-UsableCredential -Value $credential) {
                $hasCredential = $true
                break
            }
        }
        if (-not $hasCredential) {
            Open-RootEnvEditor
            $expectedNames = $credentialNames -join " or "
            throw "Configure $expectedNames in $rootEnvFile, then run the launcher again."
        }
    }
}

function Test-HttpEndpoint {
    param([Parameter(Mandatory = $true)][string]$Url)

    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    }
    catch {
        return $false
    }
}

function Get-BackendHealth {
    try {
        return Invoke-RestMethod -Uri "$backendUrl/api/health" -TimeoutSec 2
    }
    catch {
        return $null
    }
}

function Assert-BackendMode {
    param([Parameter(Mandatory = $true)]$Health)

    $provider = [string]$Health.checks.llm_provider
    $fakeProviders = @("fake", "fake_llm", "offline_llm")
    if ($Mode -eq "fake" -and $provider -notin $fakeProviders) {
        throw "Port 8001 is already serving provider '$provider', but fake mode was requested. Stop it or use -Mode real."
    }
    if ($Mode -eq "real" -and $provider -in $fakeProviders) {
        throw "Port 8001 is already serving fake LLM, but real mode was requested. Stop it and run again."
    }
    return $provider
}

function Wait-HttpEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$Attempts = 60
    )

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        if (Test-HttpEndpoint -Url $Url) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Test-ListeningPort {
    param([Parameter(Mandatory = $true)][int]$Port)

    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    return $null -ne $listener
}

function Start-ServerWindow {
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][string[]]$Commands,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $titleLiteral = ConvertTo-PowerShellLiteral -Value $Title
    $workingDirectoryLiteral = ConvertTo-PowerShellLiteral -Value $WorkingDirectory
    $payloadLines = @(
        "try { `$Host.UI.RawUI.WindowTitle = $titleLiteral } catch {}",
        "Set-Location -LiteralPath $workingDirectoryLiteral"
    ) + $Commands
    $payload = $payloadLines -join "`r`n"
    $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($payload))

    return Start-Process -FilePath "powershell.exe" `
        -ArgumentList @("-NoLogo", "-NoProfile", "-NoExit", "-EncodedCommand", $encodedCommand) `
        -WorkingDirectory $WorkingDirectory `
        -PassThru
}

Write-Host "AI Werewolf local launcher"
Write-Host "Repository : $repoRoot"
Write-Host "LLM mode   : $Mode"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Python runtime not found: $pythonPath. Create .venv and install requirements.txt first."
}

if (-not (Test-Path -LiteralPath (Join-Path $frontendDir "package.json") -PathType Leaf)) {
    throw "Frontend package.json not found: $frontendDir"
}

$npmCommand = Get-Command "npm.cmd" -ErrorAction SilentlyContinue
if ($null -eq $npmCommand) {
    $npmCommand = Get-Command "npm" -ErrorAction SilentlyContinue
}
if ($null -eq $npmCommand) {
    throw "npm was not found. Install Node.js and reopen the terminal."
}
$npmPath = $npmCommand.Source

if ($Mode -eq "real") {
    if (-not (Test-Path -LiteralPath $rootEnvFile -PathType Leaf)) {
        if (-not (Test-Path -LiteralPath $rootEnvExample -PathType Leaf)) {
            throw "Environment template not found: $rootEnvExample"
        }
        if ($CheckOnly) {
            throw "Real mode requires $rootEnvFile. A normal run will create it from $rootEnvExample."
        }
        Copy-Item -LiteralPath $rootEnvExample -Destination $rootEnvFile
        Write-Host "Created $rootEnvFile from .env.example."
        Open-RootEnvEditor
        throw "Configure the real provider and API key in $rootEnvFile, save it, then run the launcher again."
    }
    Assert-RealModeConfiguration
}

if (-not (Test-Path -LiteralPath $frontendEnvLocal -PathType Leaf)) {
    if (-not (Test-Path -LiteralPath $frontendEnvExample -PathType Leaf)) {
        throw "Frontend environment template not found: $frontendEnvExample"
    }
    if ($CheckOnly) {
        Write-Host "frontend/.env.local is missing; a normal run will create it from frontend/.env.example."
    }
    else {
        Copy-Item -LiteralPath $frontendEnvExample -Destination $frontendEnvLocal
        Write-Host "Created frontend/.env.local from frontend/.env.example"
    }
}

if (-not (Test-Path -LiteralPath $frontendModules -PathType Container)) {
    if ($CheckOnly) {
        Write-Host "Frontend dependencies are missing; a normal run will install them."
    }
    elseif ($SkipInstall) {
        throw "frontend/node_modules is missing and -SkipInstall was specified."
    }
    else {
        Write-Host "Installing frontend dependencies..."
        Push-Location $frontendDir
        try {
            & $npmPath install --legacy-peer-deps
            if ($LASTEXITCODE -ne 0) {
                throw "npm install failed with exit code $LASTEXITCODE."
            }
        }
        finally {
            Pop-Location
        }
    }
}

$backendHealth = Get-BackendHealth
$backendReady = $null -ne $backendHealth
$frontendReady = Test-HttpEndpoint -Url $frontendUrl

if ($backendReady) {
    $runningProvider = Assert-BackendMode -Health $backendHealth
}

if ($CheckOnly) {
    Write-Host "Python      : $pythonPath"
    Write-Host "npm         : $npmPath"
    Write-Host "Backend     : $(if ($backendReady) { "already running ($runningProvider)" } else { 'ready to start' })"
    Write-Host "Frontend    : $(if ($frontendReady) { 'already running' } else { 'ready to start' })"
    Write-Host "Launcher check passed."
    exit 0
}

if (-not $backendReady -and (Test-ListeningPort -Port 8001)) {
    throw "Port 8001 is occupied, but $backendUrl/api/health did not respond. Stop the conflicting process first."
}
if (-not $frontendReady -and (Test-ListeningPort -Port 3001)) {
    throw "Port 3001 is occupied, but $frontendUrl did not respond. Stop the conflicting process first."
}

$pythonLiteral = ConvertTo-PowerShellLiteral -Value $pythonPath
$npmLiteral = ConvertTo-PowerShellLiteral -Value $npmPath

if (-not $backendReady) {
    if ($Mode -eq "fake") {
        $backendEnvironment = @(
            '$env:AIWEREWOLF_SKIP_DOTENV = "true"',
            '$env:_TEST_ALLOW_FAKE_LLM = "true"',
            '$env:LLM_PROVIDER = "fake"',
            '$env:AIWEREWOLF_STRICT_MODE = "true"',
            '$env:ALLOW_FALLBACK = "false"'
        )
    }
    else {
        $backendEnvironment = @(
            'Remove-Item Env:AIWEREWOLF_SKIP_DOTENV -ErrorAction SilentlyContinue',
            'Remove-Item Env:_TEST_ALLOW_FAKE_LLM -ErrorAction SilentlyContinue',
            'if ($env:LLM_PROVIDER -in @("fake", "fake_llm", "offline_llm")) { Remove-Item Env:LLM_PROVIDER }'
        )
    }

    $backendCommands = $backendEnvironment + @(
        "& $pythonLiteral -m uvicorn backend.app:app --host 127.0.0.1 --port 8001 --reload"
    )
    $backendProcess = Start-ServerWindow `
        -Title "AI Werewolf Backend ($Mode)" `
        -Commands $backendCommands `
        -WorkingDirectory $repoRoot
    Write-Host "Started backend window (PID $($backendProcess.Id))."
}
else {
    Write-Host "Backend is already running; reusing it."
}

if (-not $frontendReady) {
    $frontendProcess = Start-ServerWindow `
        -Title "AI Werewolf Frontend" `
        -Commands @("& $npmLiteral run dev") `
        -WorkingDirectory $frontendDir
    Write-Host "Started frontend window (PID $($frontendProcess.Id))."
}
else {
    Write-Host "Frontend is already running; reusing it."
}

Write-Host "Waiting for backend and frontend..."
$backendReady = Wait-HttpEndpoint -Url "$backendUrl/api/health"
$frontendReady = Wait-HttpEndpoint -Url $frontendUrl

if (-not $backendReady) {
    throw "Backend did not become ready. Inspect the 'AI Werewolf Backend' window."
}
if (-not $frontendReady) {
    throw "Frontend did not become ready. Inspect the 'AI Werewolf Frontend' window."
}

$backendHealth = Get-BackendHealth
$runningProvider = Assert-BackendMode -Health $backendHealth

Write-Host "Backend API : $backendUrl/docs"
Write-Host "Frontend    : $frontendUrl"
Write-Host "LLM provider: $runningProvider"
Write-Host "Stop servers with Ctrl+C in their respective windows."

if (-not $NoBrowser) {
    Start-Process $frontendUrl
}
