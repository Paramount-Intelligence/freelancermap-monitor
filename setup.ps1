$ErrorActionPreference = "Stop"

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python launcher 'py' was not found. Install Python 3.11+ and enable Add Python to PATH."
}

py -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env. Add SMTP details before sending email." -ForegroundColor Yellow
}

Write-Host "Setup complete." -ForegroundColor Green
Write-Host "Next: python main.py --test-browser --visible"
Write-Host "Then: python main.py --initialize-baseline"
