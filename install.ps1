# agym Windows One-Line Installer
# Usage: irm https://raw.githubusercontent.com/Tiago-0liveira/agy-manager/main/install.ps1 | iex

$ErrorActionPreference = "Stop"

$Repo = "Tiago-0liveira/agy-manager"
$InstallBase = Join-Path $env:LOCALAPPDATA "agym"
$BinDir = Join-Path $InstallBase "bin"
$ExePath = Join-Path $BinDir "agym.exe"

Write-Host "=================================================" -ForegroundColor Cyan
Write-Host "   Installing agym (Antigravity Profile Manager) " -ForegroundColor Cyan
Write-Host "=================================================" -ForegroundColor Cyan

# 1. Create binary directory
if (-not (Test-Path $BinDir)) {
    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
}

$Installed = $false

# 2. Attempt Standalone Binary Download from Latest GitHub Release
try {
    Write-Host "Fetching latest release information from GitHub..." -ForegroundColor Yellow
    $ApiUrl = "https://api.github.com/repos/$Repo/releases/latest"
    $Headers = @{ "User-Agent" = "agym-installer-ps1" }
    
    $Release = Invoke-RestMethod -Uri $ApiUrl -Headers $Headers -TimeoutSec 15
    $Version = $Release.tag_name

    $Asset = $Release.assets | Where-Object { $env:PROCESSOR_ARCHITECTURE -eq "AMD64" -and $_.name -eq "agym-windows-amd64.exe" } | Select-Object -First 1

    if ($Asset) {
        Write-Host "Found standalone binary $Version ($($Asset.name)). Downloading..." -ForegroundColor Green
        $TempExe = Join-Path $BinDir "agym_download.tmp.exe"
        Invoke-WebRequest -Uri $Asset.browser_download_url -OutFile $TempExe -TimeoutSec 60
        
        # Replace existing binary
        if (Test-Path $ExePath) {
            $OldExe = Join-Path $BinDir "agym.exe.old"
            if (Test-Path $OldExe) { Remove-Item -Force $OldExe -ErrorAction SilentlyContinue }
            Move-Item -Path $ExePath -Destination $OldExe -Force
        }
        Move-Item -Path $TempExe -Destination $ExePath -Force
        $Installed = $true
        Write-Host "Standalone binary installed successfully ($Version)." -ForegroundColor Green
    }
} catch {
    Write-Host "Notice: Standalone binary download unavailable or failed ($($_.Exception.Message))." -ForegroundColor DarkGray
}

# 3. Fallback to Python Virtual Environment if standalone wasn't installed
if (-not $Installed) {
    Write-Host "Falling back to Python virtual environment installation..." -ForegroundColor Yellow

    $PythonExe = $null
    foreach ($candidate in @("py", "python", "python3")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) {
            $ver = & $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($ver -and [version]$ver -ge [version]"3.10") {
                $PythonExe = $candidate
                break
            }
        }
    }

    if (-not $PythonExe) {
        Write-Host "Error: Python 3.10 or higher is required when standalone binaries are unavailable." -ForegroundColor Red
        Write-Host "Please install Python 3.10+ from https://www.python.org/ or Microsoft Store." -ForegroundColor Red
        Exit 1
    }

    Write-Host "Using Python: $PythonExe" -ForegroundColor Cyan
    $VenvDir = Join-Path $InstallBase "venv"
    if (-not (Test-Path $VenvDir)) {
        Write-Host "Creating isolated virtual environment in $VenvDir..." -ForegroundColor Yellow
        & $PythonExe -m venv $VenvDir
    }

    $VenvPip = Join-Path $VenvDir "Scripts\pip.exe"
    $VenvAgym = Join-Path $VenvDir "Scripts\agym.exe"

    $Wheel = $Release.assets | Where-Object { $_.name -like "*.whl" } | Select-Object -First 1
    if ($Wheel) {
        Write-Host "Installing release wheel..." -ForegroundColor Yellow
        & $VenvPip install --upgrade $Wheel.browser_download_url
    } elseif ($Version) {
        Write-Host "Installing release tag $Version..." -ForegroundColor Yellow
        & $VenvPip install --upgrade "git+https://github.com/$Repo.git@$Version"
    } else {
        Write-Host "Installing agym from GitHub..." -ForegroundColor Yellow
        & $VenvPip install --upgrade "git+https://github.com/$Repo.git"
    }
    if ($LASTEXITCODE -ne 0) { throw "agym package installation failed" }

    # Create wrapper cmd in BinDir
    $CmdWrapper = Join-Path $BinDir "agym.cmd"
    $CmdContent = "@echo off`r`n`"$VenvAgym`" %*`r`n"
    [System.IO.File]::WriteAllText($CmdWrapper, $CmdContent)

    # Also copy or link exe if present
    if (Test-Path $VenvAgym) {
        Copy-Item -Path $VenvAgym -Destination $ExePath -Force
    }

    $Installed = $true
    Write-Host "Python environment setup complete." -ForegroundColor Green
}

# 4. PATH Configuration
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$PathEntries = $UserPath -split ";" | Where-Object { $_ -ne "" }

if ($PathEntries -notcontains $BinDir) {
    Write-Host "Adding $BinDir to User PATH environment variable..." -ForegroundColor Yellow
    $NewUserPath = "$UserPath;$BinDir"
    [Environment]::SetEnvironmentVariable("Path", $NewUserPath, "User")
    Write-Host "User PATH updated." -ForegroundColor Green
}

# Add to current PowerShell process environment
if ($env:Path -split ";" -notcontains $BinDir) {
    $env:Path = "$BinDir;$env:Path"
}

Write-Host ""
Write-Host "Installation Verified!" -ForegroundColor Green
try {
    & $ExePath --help | Select-Object -First 3
} catch {
    # If run via cmd wrapper
    & (Join-Path $BinDir "agym") --help | Select-Object -First 3
}

Write-Host ""
Write-Host "=================================================" -ForegroundColor Cyan
Write-Host "   agym has been successfully installed!         " -ForegroundColor Green
Write-Host "=================================================" -ForegroundColor Cyan
Write-Host "Location:  $BinDir" -ForegroundColor Gray
Write-Host ""
Write-Host "Quickstart:" -ForegroundColor Yellow
Write-Host "  agym setup personal      # Set up an isolated profile" -ForegroundColor White
Write-Host "  agym personal            # Launch Antigravity under profile" -ForegroundColor White
Write-Host "  agym usage               # Check live quota health" -ForegroundColor White
Write-Host "  agym update              # Update agym to latest release" -ForegroundColor White
Write-Host ""
Write-Host "(Note: If 'agym' is not recognized in existing open terminals, restart your terminal or shell)." -ForegroundColor DarkGray
