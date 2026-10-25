<#
.SYNOPSIS
    One-command Rectifi setup for Windows with PostgreSQL already installed (no Docker, no admin needed).

.DESCRIPTION
    1. finds psql and checks the PostgreSQL service is running
    2. creates the rectifi login and the three databases (runs db\init.sql as the postgres user)
    3. makes .venv and installs the pinned packages
    4. creates .env (never overwrites a value you set yourself)
    5. helps you choose a model for the Ask page (Ollama, OpenRouter free, Hugging Face, or skip)
    6. creates the tables and runs the doctor

    Safe to run again. Run it from the Rectifi folder like this (no change to your PowerShell policy needed):

        powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1

.PARAMETER Port
    The port your PostgreSQL listens on. Default 5432.

.PARAMETER Llm
    ollama | openrouter | huggingface | skip. Leave it out to be asked.

.NOTES
    This file is plain ASCII on purpose: Windows PowerShell 5.1 reads files without a BOM as ANSI.
    It never prints a password or an API key.
#>
#requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 5432,

    [string]$Llm = ''
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$MinPython = '3.11'
$AppDbPassword = 'rectifi_dev'     # the password db\init.sql gives the rectifi login (local-only database)
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONUTF8 = '1'

# ----------------------------------------------------------------------------------------------
# Output helpers
# ----------------------------------------------------------------------------------------------

function Write-Step([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text)   { Write-Host "[ok] $Text" -ForegroundColor Green }
function Write-Bad([string]$Text)  { Write-Host "[!!] $Text" -ForegroundColor Yellow }
function Write-Info([string]$Text) { Write-Host "     $Text" }

function Stop-Setup([string]$Problem, [string[]]$Fix) {
    Write-Bad $Problem
    foreach ($line in $Fix) { Write-Info $line }
    Write-Host ''
    Write-Host 'Setup stopped. It is safe to run again after you fix this.' -ForegroundColor Yellow
    exit 1
}

function Resolve-RepoPath([string]$Relative) {
    # 'db\init.sql' -> full path (also works with / on other systems, which is how the script is tested)
    Join-Path $RepoRoot ($Relative.Replace('\', [string][IO.Path]::DirectorySeparatorChar))
}

function Read-Choice([string]$Prompt, [string[]]$Valid, [string]$Default) {
    while ($true) {
        $answer = (Read-Host -Prompt "$Prompt [$Default]").Trim().ToLowerInvariant()
        if (-not $answer) { return $Default }
        if ($Valid -contains $answer) { return $answer }
        Write-Info ('Please type one of: ' + ($Valid -join ', '))
    }
}

function Read-Secret([string]$Prompt) {
    # Typed text is not shown. Returns plain text for the caller to use right away and then drop.
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

# ----------------------------------------------------------------------------------------------
# Running programs. Windows PowerShell 5.1 turns anything a program writes to stderr into an error
# when ErrorActionPreference is Stop, so programs run with it relaxed.
# ----------------------------------------------------------------------------------------------

function Invoke-Capture([string]$File, [string[]]$ProgramArgs) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $text = & $File @ProgramArgs 2>&1 | ForEach-Object { "$_" }
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = (@($text) -join "`n") }
    }
    catch { return [pscustomobject]@{ ExitCode = -1; Output = $_.Exception.Message } }
    finally { $ErrorActionPreference = $saved }
}

function Invoke-Live([string]$File, [string[]]$ProgramArgs) {
    # Shows the program's own output as it runs. Returns the exit code.
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $File @ProgramArgs | Out-Host; return $LASTEXITCODE }
    catch { Write-Info $_.Exception.Message; return -1 }
    finally { $ErrorActionPreference = $saved }
}

function Invoke-Psql([string]$Psql, [string[]]$PsqlArgs, [string]$Password) {
    # PGPASSWORD exists only while psql runs, and is always removed afterwards.
    $saved = $ErrorActionPreference
    try {
        $env:PGPASSWORD = $Password
        $ErrorActionPreference = 'Continue'
        $text = & $Psql @PsqlArgs 2>&1 | ForEach-Object { "$_" }
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = (@($text) -join "`n") }
    }
    catch { return [pscustomobject]@{ ExitCode = -1; Output = $_.Exception.Message } }
    finally {
        $ErrorActionPreference = $saved
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    }
}

# ----------------------------------------------------------------------------------------------
# .env helpers (UTF-8 without BOM, so Python reads the first line cleanly)
# ----------------------------------------------------------------------------------------------

function Get-EnvValue([string]$Path, [string]$Name) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $pattern = '^\s*' + [regex]::Escape($Name) + '\s*=(.*)$'
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ($line -match $pattern) { return $Matches[1].Trim().Trim('"').Trim("'") }
    }
    return $null
}

function Set-EnvValue([string]$Path, [string]$Name, [string]$Value, [switch]$Force, [string]$TemplateValue) {
    # Writes NAME=value. Without -Force it only replaces a value that is missing, empty, or still the
    # template's default, so a setting you changed yourself is never overwritten.
    # Returns 'set', 'same' (already that value) or 'kept' (a value you chose was left alone).
    $lines = @()
    if (Test-Path -LiteralPath $Path) { $lines = @([IO.File]::ReadAllLines($Path)) }
    $pattern = '^\s*' + [regex]::Escape($Name) + '\s*=(.*)$'
    $index = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) { $index = $i; break }
    }
    if ($index -ge 0) {
        $current = $Matches[1].Trim().Trim('"').Trim("'")
        if ($current -eq $Value) { return 'same' }
        $isDefault = (-not $current) -or ($TemplateValue -and $current -eq $TemplateValue)
        if (-not $Force -and -not $isDefault) { return 'kept' }
        $lines[$index] = "$Name=$Value"
    }
    else {
        $lines = @($lines) + "$Name=$Value"
    }
    [IO.File]::WriteAllLines($Path, [string[]]$lines, $Utf8NoBom)
    return 'set'
}

# ----------------------------------------------------------------------------------------------
# Finders
# ----------------------------------------------------------------------------------------------

function Find-Psql([string]$ProgramFilesRoot) {
    $onPath = Get-Command psql -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($onPath) { return $onPath.Source }
    $base = Join-Path $ProgramFilesRoot 'PostgreSQL'
    if (-not (Test-Path -LiteralPath $base)) { return $null }
    $best = $null
    $bestVersion = -1.0
    foreach ($dir in @(Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue)) {
        $candidate = Join-Path (Join-Path $dir.FullName 'bin') 'psql.exe'
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        $version = 0.0
        [void][double]::TryParse($dir.Name, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$version)
        if ($version -gt $bestVersion) { $best = $candidate; $bestVersion = $version }
    }
    return $best
}

function Get-ProgramFilesRoot {
    if ($env:ProgramW6432) { return $env:ProgramW6432 }      # the 64-bit folder even from 32-bit PowerShell
    if ($env:ProgramFiles) { return $env:ProgramFiles }
    return 'C:\Program Files'
}

function Find-Python {
    # Returns the full path of a Python 3.13, 3.12 or 3.11 (in that order), or $null.
    $code = 'import sys; print(sys.executable)'
    $launcher = Get-Command py -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($launcher) {
        foreach ($version in @('3.13', '3.12', '3.11')) {
            $result = Invoke-Capture $launcher.Source @("-$version", '-c', $code)
            if ($result.ExitCode -eq 0) {
                $path = @($result.Output -split "`n")[-1].Trim()
                if ($path -and (Test-Path -LiteralPath $path)) { return $path }
            }
        }
    }
    $plain = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($plain) {
        $check = "import sys; assert sys.version_info >= (3, 11); print(sys.executable)"
        $result = Invoke-Capture $plain.Source @('-c', $check)
        if ($result.ExitCode -eq 0) {
            $path = @($result.Output -split "`n")[-1].Trim()
            if ($path -and (Test-Path -LiteralPath $path)) { return $path }
        }
    }
    return $null
}

function Get-VenvPython {
    foreach ($relative in @('.venv\Scripts\python.exe', '.venv\bin\python')) {
        $path = Resolve-RepoPath $relative
        if (Test-Path -LiteralPath $path) { return $path }
    }
    return $null
}

function Find-Ollama {
    $onPath = Get-Command ollama -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($onPath) { return $onPath.Source }
    if ($env:LOCALAPPDATA) {
        $installed = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
        if (Test-Path -LiteralPath $installed) { return $installed }
    }
    return $null
}

function Select-ToolModel([string[]]$Installed) {
    # The agent needs tool calling. Return the first installed model from a family known to support it.
    foreach ($prefix in @('qwen2.5', 'qwen3', 'llama3.1', 'llama3.2', 'mistral-nemo')) {
        foreach ($name in $Installed) {
            if ($name.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -and $name -notmatch 'embed') { return $name }
        }
    }
    return $null
}

function Get-OllamaModelNames([string]$OllamaExe) {
    # Returns @{ Ok = bool; Names = string[] } from `ollama list`.
    $result = Invoke-Capture $OllamaExe @('list')
    if ($result.ExitCode -ne 0) { return @{ Ok = $false; Names = @() } }
    $names = @()
    $lines = @($result.Output -split "`n")
    for ($i = 1; $i -lt $lines.Count; $i++) {          # line 0 is the NAME / ID / SIZE header
        $first = ($lines[$i].Trim() -split '\s+')[0]
        if ($first) { $names += $first }
    }
    return @{ Ok = $true; Names = $names }
}

function Get-RamGb {
    try { return [math]::Round((Get-CimInstance Win32_ComputerSystem -ErrorAction Stop).TotalPhysicalMemory / 1GB, 1) }
    catch { return $null }
}

# ----------------------------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------------------------

function Invoke-Main {
    Set-Location -LiteralPath $RepoRoot
    $script:Llm = $script:Llm.Trim().ToLowerInvariant()
    if ($script:Llm -and (@('ollama', 'openrouter', 'huggingface', 'skip') -notcontains $script:Llm)) {
        Stop-Setup "-Llm must be one of: ollama, openrouter, huggingface, skip (you typed '$script:Llm')." @()
    }
    Write-Host 'Rectifi setup for Windows' -ForegroundColor Cyan
    Write-Info "Folder: $RepoRoot"

    # ---- a. psql and the PostgreSQL service --------------------------------------------------
    Write-Step 'PostgreSQL'
    $psql = Find-Psql (Get-ProgramFilesRoot)
    if (-not $psql) {
        Stop-Setup 'Could not find psql.exe.' @(
            'Looked on PATH and in C:\Program Files\PostgreSQL\<version>\bin.',
            'Install PostgreSQL 16 from https://www.postgresql.org/download/windows/ (the EDB installer),',
            'or, if it is installed somewhere else, add its bin folder to PATH, open a new PowerShell, and run this again.')
    }
    Write-Ok "psql: $psql"

    if (Get-Command Get-Service -ErrorAction SilentlyContinue) {
        $services = @(Get-Service -Name 'postgresql*' -ErrorAction SilentlyContinue)
        if ($services.Count -eq 0) {
            Write-Bad 'No Windows service named postgresql* was found. If you start PostgreSQL some other way, that is fine.'
        }
        else {
            $service = $services | Where-Object { $_.Name -eq 'postgresql-x64-16' } | Select-Object -First 1
            if (-not $service) { $service = $services | Where-Object { $_.Status -eq 'Running' } | Select-Object -First 1 }
            if (-not $service) { $service = $services | Select-Object -First 1 }
            if ($service.Status -eq 'Running') {
                Write-Ok "Service $($service.Name) is running"
            }
            else {
                Write-Bad "Service $($service.Name) is $($service.Status). Trying to start it ..."
                try {
                    Start-Service -Name $service.Name -ErrorAction Stop
                    Write-Ok "Service $($service.Name) started"
                }
                catch {
                    Stop-Setup "Could not start the service $($service.Name) (starting a service needs admin rights)." @(
                        'Open PowerShell as administrator (Start menu, type PowerShell, right-click, Run as administrator) and run:',
                        "    Start-Service -Name '$($service.Name)'",
                        'Then run this setup again in your normal PowerShell.')
                }
            }
        }
    }

    # ---- b. the rectifi login and databases ---------------------------------------------------
    Write-Step 'Database login and databases'
    $initSql = Resolve-RepoPath 'db\init.sql'
    if (-not (Test-Path -LiteralPath $initSql)) {
        Stop-Setup "db\init.sql is missing ($initSql)." @('Run this script from a complete copy of the Rectifi folder.')
    }
    $psqlArgs = @('-X', '-w', '-h', 'localhost', '-p', "$Port", '-U', 'postgres', '-d', 'postgres',
                  '-v', 'ON_ERROR_STOP=1', '-q', '-f', $initSql)
    $postgresPassword = $null
    $done = $false
    try {
        $attempt = 1
        $waits = 0
        if ($env:PGPASSWORD) {
            $postgresPassword = $env:PGPASSWORD
            Write-Ok 'Using PGPASSWORD from your environment for the postgres user (first try only)'
        }
        while ($attempt -le 3 -and -not $done) {
            if (-not $postgresPassword) {
                $postgresPassword = Read-Secret 'Password of the PostgreSQL "postgres" user (the one you chose when you installed it)'
                if (-not $postgresPassword) { Write-Info 'The password cannot be empty.'; continue }
            }
            $result = Invoke-Psql $psql $psqlArgs $postgresPassword
            if ($result.ExitCode -eq 0) { $done = $true; break }

            $text = $result.Output
            if ($text -match 'could not connect|Connection refused|connection refused|could not translate|timeout expired|Connection timed out|starting up|No connection could be made') {
                if ($waits -lt 5 -and $text -match 'starting up') {
                    $waits++; Write-Info 'PostgreSQL is still starting. Waiting 3 seconds ...'; Start-Sleep -Seconds 3; continue
                }
                Stop-Setup "Could not connect to PostgreSQL on localhost port $Port." @(
                    'Is the service running, and is that the right port? Find the port in the installer notes or in',
                    '"C:\Program Files\PostgreSQL\16\data\postgresql.conf" (line "port = ...").',
                    'If it is not 5432, run:  powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -Port <number>')
            }
            if ($result.ExitCode -eq 2) {
                Write-Bad "Login failed (attempt $attempt of 3). That password was not accepted for the postgres user."
                $postgresPassword = $null
                $attempt++
                continue
            }
            Stop-Setup 'db\init.sql failed.' @(($text -split "`n" | Select-Object -Last 8))
        }
        if (-not $done) {
            Stop-Setup 'Three wrong passwords. Nothing was changed.' @(
                'Use the password you chose for the postgres user when you installed PostgreSQL.',
                'If you forgot it, you can reset it: https://www.postgresql.org/docs/16/auth-pg-hba-conf.html (trust method),',
                'or in pgAdmin right-click Login/Group Roles, postgres, Properties, Definition. Then run this again.')
        }
    }
    finally {
        $postgresPassword = $null
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    }
    Write-Ok 'Login "rectifi" and databases rectifi, rectifi_test, rectifi_demo are ready (existing ones were left alone)'

    $probe = Invoke-Psql $psql @('-X', '-w', '-h', 'localhost', '-p', "$Port", '-U', 'rectifi', '-d', 'rectifi', '-tA', '-c', 'SELECT 1') $AppDbPassword
    if ($probe.ExitCode -eq 0) { Write-Ok 'The rectifi login can connect' }
    else { Write-Bad 'The rectifi login could not connect. If you changed its password earlier, put the new one in DATABASE_URL in .env.' }

    # ---- c. Python and packages --------------------------------------------------------------
    Write-Step 'Python and packages'
    $venvPython = Get-VenvPython
    if ($venvPython) {
        Write-Ok '.venv already exists'
    }
    else {
        $python = Find-Python
        if (-not $python) {
            Stop-Setup "Could not find Python $MinPython or newer." @(
                'Install Python 3.13 from https://www.python.org/downloads/ . In the installer keep "py launcher" ticked',
                '(and tick "Add python.exe to PATH"). Then open a new PowerShell and run this again.')
        }
        $versionText = (Invoke-Capture $python @('-c', "import sys; print('%d.%d' % sys.version_info[:2])")).Output.Trim()
        Write-Ok "Python $versionText ($python)"
        if ($versionText -ne '3.13') { Write-Bad 'Rectifi was tested on Python 3.13. Other versions from 3.11 should work but were not tested.' }
        Write-Info 'Creating .venv ...'
        $code = Invoke-Live $python @('-m', 'venv', (Resolve-RepoPath '.venv'))
        $venvPython = Get-VenvPython
        if ($code -ne 0 -or -not $venvPython) { Stop-Setup 'Could not create the virtual environment (.venv).' @('Delete the .venv folder if it exists, then run this again.') }
        Write-Ok '.venv created'
    }
    Write-Info 'Installing packages. The first time this takes a few minutes ...'
    if ((Invoke-Live $venvPython @('-m', 'pip', 'install', '--quiet', '--upgrade', 'pip')) -ne 0) {
        Write-Bad 'Could not upgrade pip (continuing with the pip that came with Python).'
    }
    if ((Invoke-Live $venvPython @('-m', 'pip', 'install', '--quiet', '-r', (Resolve-RepoPath 'requirements.txt'))) -ne 0) {
        Stop-Setup 'Installing the packages failed.' @(
            'Read the red lines above. The usual cause is no internet connection; check it and run this again.')
    }
    Write-Ok 'Packages installed'

    # ---- d. .env -----------------------------------------------------------------------------
    Write-Step '.env settings'
    $envPath = Resolve-RepoPath '.env'
    $examplePath = Resolve-RepoPath '.env.example'
    if (-not (Test-Path -LiteralPath $envPath)) {
        Copy-Item -LiteralPath $examplePath -Destination $envPath
        Write-Ok '.env created from .env.example'
    }
    else {
        Write-Ok '.env already exists (your own values are kept)'
    }
    $dbUrl = "postgresql://rectifi:$AppDbPassword@localhost:$Port/rectifi"
    $testUrl = "postgresql://rectifi:$AppDbPassword@localhost:$Port/rectifi_test"
    foreach ($pair in @(@('DATABASE_URL', $dbUrl), @('TEST_DATABASE_URL', $testUrl))) {
        $template = Get-EnvValue $examplePath $pair[0]
        $state = Set-EnvValue $envPath $pair[0] $pair[1] -TemplateValue $template
        if ($state -eq 'kept') { Write-Bad "$($pair[0]) was set by you to something else, so it was left alone. Check it uses port $Port." }
        else { Write-Ok "$($pair[0]) points at localhost port $Port" }
    }

    # ---- e. the model for the Ask page -------------------------------------------------------
    Write-Step 'Model for the Ask page (optional)'
    Invoke-LlmChoice $envPath

    # ---- f. tables and doctor ----------------------------------------------------------------
    Write-Step 'Tables and check'
    if ((Invoke-Live $venvPython @((Resolve-RepoPath 'scripts\dev.py'), 'init')) -ne 0) {
        Stop-Setup 'Creating the tables failed. Read the message above.' @('Then run this setup again.')
    }
    Write-Ok 'Tables are ready'
    Write-Host ''
    $doctorCode = Invoke-Live $venvPython @((Resolve-RepoPath 'scripts\dev.py'), 'doctor')
    Write-Host ''
    if ($doctorCode -eq 0) { Write-Ok 'Doctor says everything is ready' }
    else { Write-Bad 'Doctor found something to fix (see the lines above). The app may still start.' }

    Write-Host ''
    Write-Host 'Done. Start Rectifi with:' -ForegroundColor Cyan
    Write-Host '    .venv\Scripts\python.exe scripts\dev.py up' -ForegroundColor White
    Write-Info 'Then open http://localhost:8501 in your browser.'
    Write-Info 'You do not need to activate the virtual environment. If you want to, run:  .venv\Scripts\Activate.ps1'
    Write-Info '(if PowerShell blocks that, use  .venv\Scripts\activate.bat  from Command Prompt, or just keep using the full path above).'
    Write-Info 'Try the app with fake data first:  .venv\Scripts\python.exe scripts\dev.py demo'
}

function Test-LlmConfigured([string]$EnvPath) {
    $provider = Get-EnvValue $EnvPath 'LLM_PROVIDER'
    switch ($provider) {
        'deepseek'      { return [bool](Get-EnvValue $EnvPath 'DEEPSEEK_API_KEY') }
        'openrouter'    { return ([bool](Get-EnvValue $EnvPath 'OPENROUTER_API_KEY') -and [bool](Get-EnvValue $EnvPath 'OPENROUTER_MODEL')) }
        'huggingface'   { return [bool](Get-EnvValue $EnvPath 'HF_TOKEN') }
        'openai_compat' { return ([bool](Get-EnvValue $EnvPath 'OPENAI_COMPAT_API_KEY') -and [bool](Get-EnvValue $EnvPath 'OPENAI_COMPAT_MODEL')) }
        default         { return $false }
    }
}

function Invoke-LlmChoice([string]$EnvPath) {
    $choice = $script:Llm
    $ollamaExe = Find-Ollama
    if (-not $choice) {
        if (Test-LlmConfigured $EnvPath) {
            $current = Get-EnvValue $EnvPath 'LLM_PROVIDER'
            Write-Ok "The Ask page is already set up in .env (provider: $current). To change it run this script with -Llm ollama, openrouter or huggingface."
            return
        }
        Write-Host '  1) Ollama      free, private, runs on this PC (needs RAM; a 4.7 GB download)'
        Write-Host '  2) OpenRouter  free models, but only 50 requests a day; your messages leave this PC'
        Write-Host '  3) Hugging Face  NOT free on a free account (needs PRO or bought credits)'
        Write-Host '  4) Skip        set it up later; everything except the Ask page works'
        $default = '2'
        if ($ollamaExe) { $default = '1' }
        $picked = Read-Choice 'Choose 1, 2, 3 or 4' @('1', '2', '3', '4') $default
        $choice = @{ '1' = 'ollama'; '2' = 'openrouter'; '3' = 'huggingface'; '4' = 'skip' }[$picked]
    }

    switch ($choice) {
        'skip' {
            Write-Ok 'Skipped. The Ask page will say what to do. Run this script again with -Llm ollama (or openrouter) any time.'
        }
        'ollama' { Set-UpOllama $EnvPath $ollamaExe }
        'openrouter' { Set-UpOpenRouter $EnvPath }
        'huggingface' { Set-UpHuggingFace $EnvPath }
    }
}

function Set-UpOllama([string]$EnvPath, [string]$OllamaExe) {
    if (-not $OllamaExe) {
        Write-Bad 'Ollama is not installed.'
        Write-Info 'Install it from https://ollama.com/download , open it once, and run this script again with -Llm ollama.'
        Write-Info 'Or choose OpenRouter:  -Llm openrouter'
        return
    }
    Write-Ok "Ollama: $OllamaExe"
    $listing = Get-OllamaModelNames $OllamaExe
    if (-not $listing.Ok) {
        Write-Bad 'Ollama is installed but not answering. Open the Ollama app from the Start menu and wait a few seconds.'
        [void](Read-Host -Prompt 'Press Enter to try again')
        $listing = Get-OllamaModelNames $OllamaExe
        if (-not $listing.Ok) {
            Write-Bad 'Still no answer from Ollama. Skipping; run this script again with -Llm ollama once Ollama is open.'
            return
        }
    }
    $model = Select-ToolModel $listing.Names
    if ($model) {
        Write-Ok "Found a model that supports tools: $model"
    }
    else {
        Write-Bad 'No tool-capable model is downloaded yet (the agent needs tool calling: qwen2.5, qwen3, llama3.1, llama3.2 or mistral-nemo).'
        $ram = Get-RamGb
        if ($ram) { Write-Info "This PC has about $ram GB of RAM." }
        Write-Host '  1) qwen2.5:7b   best answers; about 4.7 GB to download; needs about 8 GB of free RAM'
        Write-Host '  2) qwen2.5:3b   smaller, about 2 GB; for laptops with little RAM; makes more mistakes with tools'
        Write-Host '  3) none         skip'
        $default = '1'
        if ($ram -and $ram -lt 10) { $default = '2' }
        $picked = Read-Choice 'Download which one? 1, 2 or 3' @('1', '2', '3') $default
        if ($picked -eq '3') { Write-Ok 'Skipped the model download.'; return }
        $model = @{ '1' = 'qwen2.5:7b'; '2' = 'qwen2.5:3b' }[$picked]
        Write-Info "Running: ollama pull $model (this can take a while)"
        if ((Invoke-Live $OllamaExe @('pull', $model)) -ne 0) {
            Write-Bad "ollama pull $model failed. Check your internet connection and run this script again with -Llm ollama."
            return
        }
        Write-Ok "Downloaded $model"
    }
    [void](Set-EnvValue $EnvPath 'LLM_PROVIDER' 'ollama' -Force)
    [void](Set-EnvValue $EnvPath 'OLLAMA_MODEL' $model -Force)
    Write-Ok "The Ask page will use Ollama with $model"
}

function Set-UpOpenRouter([string]$EnvPath) {
    Write-Info 'OpenRouter free models: about 20 requests a minute and 50 a day. Your messages go to OpenRouter and the model host.'
    Write-Info 'Make a key at https://openrouter.ai/keys (no card needed for :free models).'
    $existing = Get-EnvValue $EnvPath 'OPENROUTER_API_KEY'
    $hint = ''
    if ($existing) { $hint = ' (press Enter to keep the one in .env)' }
    $key = Read-Secret "Paste your OpenRouter API key$hint"
    if ($key) { [void](Set-EnvValue $EnvPath 'OPENROUTER_API_KEY' $key -Force) }
    elseif (-not $existing) { Write-Bad 'No key entered. Skipping OpenRouter.'; return }
    $key = $null
    [void](Set-EnvValue $EnvPath 'LLM_PROVIDER' 'openrouter' -Force)
    if ($key) { Write-Ok 'Key saved to .env (not shown)' } else { Write-Ok 'Keeping the key already in .env' }

    Write-Info 'Asking OpenRouter which free models support tools (this needs internet) ...'
    $venvPython = Get-VenvPython
    $code = Invoke-Live $venvPython @((Resolve-RepoPath 'scripts\dev.py'), 'free-models')
    if ($code -ne 0) {
        Write-Bad 'Could not get the list. Later, run:  .venv\Scripts\python.exe scripts\dev.py free-models  and put a model id in OPENROUTER_MODEL in .env.'
        return
    }
    $model = (Read-Host -Prompt 'Type or paste one model id from the list (Enter to choose later)').Trim()
    if (-not $model) {
        Write-Bad 'No model chosen. Put one in OPENROUTER_MODEL in .env before using the Ask page.'
        return
    }
    if (-not $model.EndsWith(':free')) {
        Write-Bad "'$model' does not end in :free, so it may use paid credits."
        if ((Read-Choice 'Use it anyway? (y/n)' @('y', 'n') 'n') -ne 'y') {
            Write-Bad 'Not saved. Put a :free model in OPENROUTER_MODEL in .env.'
            return
        }
    }
    [void](Set-EnvValue $EnvPath 'OPENROUTER_MODEL' $model -Force)
    Write-Ok "The Ask page will use OpenRouter with $model"
    Write-Info 'Free model lists change. If a model stops working, run dev.py free-models again and pick another.'
}

function Set-UpHuggingFace([string]$EnvPath) {
    Write-Bad 'Hugging Face is NOT free on a free account: free accounts get $0 of Inference Providers credit.'
    Write-Info 'It works only with PRO ($2 of credit a month) or credits you bought. Otherwise every question fails with HTTP 402.'
    if ((Read-Choice 'Do you have PRO or purchased credits? (y/n)' @('y', 'n') 'n') -ne 'y') {
        Write-Ok 'Skipped Hugging Face. Ollama or OpenRouter :free cost nothing: run this script again with -Llm ollama or -Llm openrouter.'
        return
    }
    Write-Info 'Make a fine-grained token at https://huggingface.co/settings/tokens with "Make calls to Inference Providers".'
    $existing = Get-EnvValue $EnvPath 'HF_TOKEN'
    $hint = ''
    if ($existing) { $hint = ' (press Enter to keep the one in .env)' }
    $token = Read-Secret "Paste your Hugging Face token$hint"
    if ($token) { [void](Set-EnvValue $EnvPath 'HF_TOKEN' $token -Force) }
    elseif (-not $existing) { Write-Bad 'No token entered. Skipping Hugging Face.'; return }
    $token = $null
    [void](Set-EnvValue $EnvPath 'LLM_PROVIDER' 'huggingface' -Force)
    $model = Get-EnvValue $EnvPath 'HF_MODEL'
    if (-not $model) { $model = 'deepseek-ai/DeepSeek-V3.1:cheapest'; [void](Set-EnvValue $EnvPath 'HF_MODEL' $model -Force) }
    Write-Ok "The Ask page will use Hugging Face with $model (token saved to .env, not shown)"
}

# Run unless the file is being loaded only to test its functions (RECTIFI_SETUP_NO_RUN=1).
if ($env:RECTIFI_SETUP_NO_RUN -ne '1') { Invoke-Main }
