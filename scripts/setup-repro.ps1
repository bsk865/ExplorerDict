# 10 分钟复现脚本（Windows PowerShell 5.1+）
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\setup-repro.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\setup-repro.ps1 -SkipModel
# 作用：检查 Python、创建 .build-env、安装依赖、下载并校验离线向量模型。
# 不改动任何应用代码与 data/ 数据；可重复执行。

[CmdletBinding()]
param(
    [string]$PythonCommand = "python",
    [switch]$SkipModel,
    [string]$ModelMirror = "https://huggingface.co",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Write-Step([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }

Write-Step "检查 Python"
$version = & $PythonCommand -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($LASTEXITCODE -ne 0) { throw "找不到可用的 Python，请先安装 Python 3.12+ 并确保 python 在 PATH 中。" }
$major, $minor = $version.Split('.')
if ([int]$major -lt 3 -or ([int]$major -eq 3 -and [int]$minor -lt 12)) {
    throw "需要 Python 3.12 或更高版本，当前为 $version。"
}
Write-Host "Python $version OK"

$venv = Join-Path $Root ".build-env"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Step "创建虚拟环境 .build-env"
    & $PythonCommand -m venv $venv
}
$venvPython = Join-Path $venv "Scripts\python.exe"

Write-Step "安装依赖（requirements.txt）"
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败，请检查网络或 pip 源。" }

if (-not $SkipModel) {
    Write-Step "准备离线向量模型（Xenova/multilingual-e5-small）"
    $modelDir = Join-Path $Root "models\multilingual-e5-small"
    New-Item -ItemType Directory -Force -Path $modelDir | Out-Null
    $revision = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
    $files = @(
        @{
            Path = "onnx\model_quantized.onnx"
            Url  = "$ModelMirror/Xenova/multilingual-e5-small/resolve/$revision/onnx/model_quantized.onnx"
            Sha  = "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193"
        },
        @{
            Path = "tokenizer.json"
            Url  = "$ModelMirror/Xenova/multilingual-e5-small/resolve/$revision/tokenizer.json"
            Sha  = "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"
        }
    )
    foreach ($item in $files) {
        $target = Join-Path $modelDir $item.Path
        $ok = (Test-Path $target) -and ((Get-FileHash $target -Algorithm SHA256).Hash.ToLower() -eq $item.Sha)
        if ($ok) { Write-Host ("已存在且校验通过：" + $item.Path); continue }
        Write-Host ("下载 " + $item.Path + " ...")
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        $tmp = "$target.download"
        Invoke-WebRequest -Uri $item.Url -OutFile $tmp -UseBasicParsing
        $hash = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
        if ($hash -ne $item.Sha) { Remove-Item $tmp -Force; throw ("校验失败：" + $item.Path + "，期望 " + $item.Sha + "，实际 " + $hash) }
        Move-Item -Force $tmp $target
        Write-Host ("校验通过：" + $item.Path)
    }
} else {
    Write-Host "`n已跳过模型下载；没有模型时语义检索不可用，其余本地功能正常。" -ForegroundColor Yellow
}

if (-not $SkipTests) {
    Write-Step "冒烟测试（完整测试约 90 秒）"
    & $venvPython -X utf8 -m unittest discover -s tests -t .
    if ($LASTEXITCODE -ne 0) { throw "测试未全部通过，请查看输出。" }
}

Write-Step "构建本地 UIA 辅助库"
& (Join-Path $Root "tools/build_uia_native.ps1")

Write-Step "完成"
Write-Host "启动应用： $venvPython native_desktop.py"
Write-Host "详细步骤与验收清单： docs\复现指南.md"
