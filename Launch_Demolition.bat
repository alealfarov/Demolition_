@echo off
cd /d "%~dp0"

echo Starting BAG Demolition Explorer...

docker info >nul 2>&1
if errorlevel 1 (
    echo ERROR: Start Docker Desktop first.
    pause
    exit /b 1
)

if not exist ".streamlit\secrets.toml" (
    echo ERROR: Missing .streamlit\secrets.toml
    echo Configure database credentials before launching.
    pause
    exit /b 1
)

docker build -t bag-demolition-explorer:local .
if errorlevel 1 (
    echo Docker build failed.
    pause
    exit /b 1
)

echo.
echo Open http://localhost:8504 in your browser.
echo Press Ctrl+C to stop the application.
echo.

docker run --rm -p 127.0.0.1:8504:8501 --mount "type=bind,source=%CD%\.streamlit\secrets.toml,target=/app/.streamlit/secrets.toml,readonly" bag-demolition-explorer:local

pause
