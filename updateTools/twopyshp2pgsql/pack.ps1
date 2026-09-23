#Requires -Version 5.1
$ErrorActionPreference = "Stop"

$ProjectDir = $PSScriptRoot
$PackageName = "twopyshp2pgsql"
$DistDir = Join-Path $ProjectDir "dist"
$ZipPath = Join-Path $DistDir "$PackageName-portable.zip"
$StageRoot = Join-Path $env:TEMP ("pack-" + [guid]::NewGuid().ToString("N"))
$StageDir = Join-Path $StageRoot $PackageName

New-Item -ItemType Directory -Force -Path $StageDir | Out-Null
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

Write-Host "复制文件（排除 .venv、logs、dist 等）..." -ForegroundColor Cyan
robocopy $ProjectDir $StageDir /E /XD .venv __pycache__ logs .waylog dist /XF *.zip .env /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy 复制失败，退出码 $LASTEXITCODE" }

if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }

Write-Host "生成压缩包 $ZipPath ..." -ForegroundColor Cyan
Compress-Archive -Path $StageDir -DestinationPath $ZipPath -CompressionLevel Optimal
Remove-Item -LiteralPath $StageRoot -Recurse -Force

Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
$total = $zip.Entries.Count
$venvCount = @($zip.Entries | Where-Object { $_.FullName -like "$PackageName/.venv/*" }).Count
$zip.Dispose()

$sizeMb = [math]::Round((Get-Item -LiteralPath $ZipPath).Length / 1MB, 2)
Write-Host "打包完成: $ZipPath" -ForegroundColor Green
Write-Host ("条目数: {0}，大小: {1} MB，.venv 条目: {2}" -f $total, $sizeMb, $venvCount)

if ($venvCount -gt 0) {
    Write-Host "警告：压缩包内仍包含 .venv，请检查排除规则" -ForegroundColor Red
    exit 1
}
