#Requires -Version 5.1
param(
    [Parameter(Mandatory = $true, HelpMessage = "要更新的区域目录名，可传多个")]
    [string[]]$Region,

    [string]$Root = "",

    [string]$Continent = "",

    [string]$Country = "",

    [string]$LogDir = ""
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$ProjectDir = $PSScriptRoot
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$Script = Join-Path $ProjectDir "main3_region.py"

function Test-VenvPython {
    if (-not (Test-Path -LiteralPath $VenvPython)) { return $false }

    $ErrorActionPreference = "Continue"
    & $VenvPython -c "import sys" 2>$null
    return ($LASTEXITCODE -eq 0)
}

if (-not (Test-VenvPython)) {
    Write-Host "当前 .venv 不可用；它可能是从其他机器复制来的。" -ForegroundColor Red
    Write-Host "请在本目录运行以下命令，安装脚本会自动清理并重建环境：" -ForegroundColor Yellow
    Write-Host "  powershell -ExecutionPolicy Bypass -File .\setup.ps1"
    exit 1
}

if (-not $LogDir) {
    $LogDir = Join-Path $ProjectDir "logs"
}

$PythonArgs = @($Script)
if ($Continent) { $PythonArgs += @("--continent", $Continent) }
if ($Country) { $PythonArgs += @("--country", $Country) }
$PythonArgs += @("--region") + $Region
if ($Root) { $PythonArgs += @("--root", $Root) }
$PythonArgs += @("--log-dir", $LogDir)

Write-Host ("执行: {0} {1}" -f $VenvPython, ($PythonArgs -join " ")) -ForegroundColor DarkGray

& $VenvPython @PythonArgs
exit $LASTEXITCODE
