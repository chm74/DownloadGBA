#Requires -Version 5.1
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "=== twopyshp2pgsql 环境安装 ===" -ForegroundColor Cyan

$ProjectDir = $PSScriptRoot
$VenvDir = Join-Path $ProjectDir ".venv"
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$Requirements = Join-Path $ProjectDir "requirements.txt"

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Get-PythonVersion {
    param([string]$Exe, [string[]]$PreArgs = @())
    $ErrorActionPreference = "Continue"
    $output = & $Exe @PreArgs -c "import sys; print('{0}.{1}'.format(sys.version_info[0], sys.version_info[1]))" 2>$null
    if ($LASTEXITCODE -eq 0 -and $output) {
        return ([string]$output).Trim()
    }
    return $null
}

# Windows 虚拟环境会记录创建它的机器上的 Python 绝对路径，不能直接复制。
# 如果检测到残缺或来自其他机器的 .venv，先清理，再由下面的流程重建。
if (Test-Path -LiteralPath $VenvDir) {
    $venvVersion = $null
    if (Test-Path -LiteralPath $VenvPython) {
        $venvVersion = Get-PythonVersion $VenvPython
    }

    if (-not $venvVersion) {
        Write-Host "检测到不可用的 .venv（通常是从其他机器复制而来），正在清理 ..." -ForegroundColor Yellow
        Remove-Item -LiteralPath $VenvDir -Recurse -Force
    }
    else {
        Write-Host "检测到可用的现有虚拟环境：Python $venvVersion" -ForegroundColor Green
    }
}

if (Test-CommandExists "uv") {
    Write-Host "[1/2] 检测到 uv，按 uv.lock 同步依赖 ..." -ForegroundColor Green
    uv sync --project $ProjectDir
    if ($LASTEXITCODE -ne 0) { throw "uv sync 执行失败" }
}
else {
    Write-Host "[1/2] 未检测到 uv，改用 Python 自带的 venv + pip ..." -ForegroundColor Yellow

    $Launcher = $null
    $LauncherArgs = @()

    if (Test-CommandExists "py") {
        $version = Get-PythonVersion "py" @("-3.12")
        if ($version) {
            $Launcher = "py"
            $LauncherArgs = @("-3.12")
            Write-Host "使用 Python $version (py -3.12)"
        }
    }

    if (-not $Launcher -and (Test-CommandExists "python")) {
        $version = Get-PythonVersion "python"
        if ($version) {
            $parts = $version.Split(".")
            if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 12) {
                $Launcher = "python"
                Write-Host "使用 Python $version (python)"
            }
        }
    }

    if (-not $Launcher) {
        Write-Host "未找到可用的 Python 3.12 及以上版本。" -ForegroundColor Red
        Write-Host "请任选一种方式安装后重新运行本脚本："
        Write-Host "  winget install Python.Python.3.12"
        Write-Host "  winget install astral-sh.uv"
        Write-Host "或从 https://www.python.org/downloads/ 下载安装（勾选 Add python.exe to PATH）"
        exit 1
    }

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        Write-Host "创建虚拟环境 .venv ..."
        & $Launcher @LauncherArgs -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw "创建虚拟环境失败" }
    }

    & $VenvPython -m pip install --upgrade pip
    & $VenvPython -m pip install -r $Requirements
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败，请检查网络后重试" }
}

Write-Host "[2/2] 环境就绪。" -ForegroundColor Green
Write-Host ""
Write-Host "运行示例："
Write-Host '  .\run.ps1 -Region 广西壮族自治区 -Root D:\YZ\1.0.116\BuildingData\AutoGenerate'
Write-Host ""
Write-Host "如提示禁止运行脚本，请改用："
Write-Host "  powershell -ExecutionPolicy Bypass -File .\setup.ps1"
