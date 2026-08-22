# ==============================================================================
# Blackboard Scraper CLI Windows PowerShell Installer
# Installs 'bbscraper', 'blackboard', and 'bb' into $HOME\.local\bin
# ==============================================================================

$ErrorActionPreference = "Stop"

$ProjectDir = $PSScriptRoot
$BinDir = Join-Path $env:USERPROFILE ".local\bin"
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"

Write-Host "🎓 Installing Blackboard Scraper Global CLI on Windows..." -ForegroundColor Cyan
Write-Host "   ↳ Project Directory: $ProjectDir"
Write-Host "   ↳ Target Bin Dir:    $BinDir"

# 1. Verify virtual environment python exists
if (-not (Test-Path $VenvPython)) {
    Write-Host "❌ Error: Virtual environment not found at $VenvPython" -ForegroundColor Red
    Write-Host "   Please create it first:"
    Write-Host "   python -m venv .venv"
    Write-Host "   .\.venv\Scripts\pip install -r requirements.txt"
    Write-Host "   .\.venv\Scripts\playwright install chromium"
    exit 1
}

# 2. Ensure target bin directory exists
if (-not (Test-Path $BinDir)) {
    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
}

# 3. Create cmd wrapper scripts
$Commands = @("bb", "blackboard", "bbscraper")

foreach ($cmd in $Commands) {
    $CmdFile = Join-Path $BinDir "$cmd.cmd"
    $Content = @"
@echo off
"$VenvPython" "$ProjectDir\main.py" %*
"@
    Set-Content -Path $CmdFile -Value $Content -Encoding ASCII
    Write-Host "   • $cmd.cmd -> $CmdFile" -ForegroundColor Green
}

# 4. Check PATH
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$BinDir*") {
    Write-Host "`n⚠️ Note: $BinDir is not in your User PATH." -ForegroundColor Yellow
    Write-Host "   To add it automatically, run:" -ForegroundColor Yellow
    Write-Host "   [Environment]::SetEnvironmentVariable('Path', `$UserPath + ';$BinDir', 'User')" -ForegroundColor White
}

Write-Host "`n✨ Successfully installed global Windows CLI wrappers!" -ForegroundColor Green
Write-Host "🚀 You can now run 'bb', 'blackboard', or 'bbscraper' from any terminal!" -ForegroundColor Cyan
