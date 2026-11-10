$ErrorActionPreference = "Stop"

if (-not (Test-Path ".\.env")) {
    Copy-Item ".\.env.example" ".\.env"
    Write-Host "Created .env. Add your BotFather token, then run again."
    exit 1
}
if (-not (Test-Path ".\database\mpsc_subject_only.db")) {
    throw "Missing database\mpsc_subject_only.db. Copy the supplied subject-only DB into database\."
}
if (-not (Test-Path ".\.venv")) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe .\bot\bot.py
