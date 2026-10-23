# Optional local overrides. No API credentials belong in this file.
# Copy commands into your PowerShell session only when needed.
$env:EXPLORER_DICT_DATA_DIR = Join-Path $PWD '.tmp/repro-data'
$env:EXPLORER_DICT_STORAGE_CONFIG = Join-Path $PWD '.tmp/repro-storage.json'
# For headless tests only; remove this before launching the visible application:
# $env:QT_QPA_PLATFORM = 'offscreen'
# Configure provider URL, model, reasoning effort and key in the application settings.
