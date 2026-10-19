$ErrorActionPreference = 'Stop'
$readingRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $readingRoot
& '.build-env\Scripts\python.exe' -m PyInstaller --clean --noconfirm --distpath artifacts/reading-package --workpath artifacts/reading-build ReadingWorkspace.spec
if ($LASTEXITCODE -ne 0) { throw 'Reading workspace build failed' }
