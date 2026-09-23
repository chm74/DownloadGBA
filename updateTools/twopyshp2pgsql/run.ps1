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

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "未找到虚拟环境 .venv，请先运行 .\setup.ps1" -ForegroundColor Red
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
