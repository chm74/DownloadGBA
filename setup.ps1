#Requires -Version 5.1
param(
    [switch]$SkipTools,
    [switch]$SkipMain
)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "=== DownloadGBA 环境一键安装 ===" -ForegroundColor Cyan

$RepoRoot = $PSScriptRoot
$Tools = @(
    @{ Name = "入库工具 Twopyshp2pgsql"; Dir = Join-Path $RepoRoot "Tools\Twopyshp2pgsql"; Setup = "setup.ps1" },
    @{ Name = "处理工具 Oneshp_pipline_qgis"; Dir = Join-Path $RepoRoot "Tools\Oneshp_pipline_qgis"; Setup = "setup.ps1" }
)

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

if (-not $SkipTools) {
    if (Test-CommandExists "uv") {
        Write-Host "检测到 uv，使用 uv sync 创建两个工具环境 ..." -ForegroundColor Green
        uv sync --project (Join-Path $RepoRoot "Tools\Twopyshp2pgsql")
        if ($LASTEXITCODE -ne 0) { throw "uv sync Tools\Twopyshp2pgsql 失败" }
        uv sync --project (Join-Path $RepoRoot "Tools\Oneshp_pipline_qgis")
        if ($LASTEXITCODE -ne 0) { throw "uv sync Tools\Oneshp_pipline_qgis 失败" }
    }
    else {
        Write-Host "未检测到 uv，调用各工具 setup.ps1（Python venv + pip）..." -ForegroundColor Yellow
        foreach ($tool in $Tools) {
            $setupScript = Join-Path $tool.Dir $tool.Setup
            if (Test-Path -LiteralPath $setupScript) {
                Write-Host (">> 安装 {0}" -f $tool.Name) -ForegroundColor DarkGray
                & powershell -ExecutionPolicy Bypass -File $setupScript
                if ($LASTEXITCODE -ne 0) { throw ("{0} 安装失败" -f $tool.Name) }
            }
            else {
                Write-Host ("跳过 {0}：未找到 {1}" -f $tool.Name, $setupScript) -ForegroundColor Yellow
            }
        }
    }
}

if (-not $SkipMain) {
    $req = Join-Path $RepoRoot "requirements-main.txt"
    if (-not (Test-Path -LiteralPath $req)) { throw "未找到 requirements-main.txt" }
    $pb = $null
    if (Test-CommandExists "py") { $pb = "py" }
    elseif (Test-CommandExists "python") { $pb = "python" }
    if (-not $pb) { throw "未找到 Python，请先安装 Python 3.12" }
    Write-Host "安装主下载链依赖（系统 Python）..." -ForegroundColor Green
    & $pb -m pip install --upgrade pip
    & $pb -m pip install -r $req
    if ($LASTEXITCODE -ne 0) { throw "主下载链依赖安装失败" }
}

Write-Host ""
Write-Host "完成。下一步：" -ForegroundColor Cyan
Write-Host "  1) 启动看板： 启动.bat"
Write-Host "  2) 打开 http://127.0.0.1:8765/ -> 「配置」页填写 数据库连接 / 共享目录 / QGIS 安装目录"
Write-Host "  3) 「配置 -> 环境 / 诊断 -> 检测环境」确认全绿"
Write-Host ""
Write-Host "注：.venv 不可跨机复制，均在本机重建，无需提交仓库。"
