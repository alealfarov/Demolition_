# BAG Demolition Explorer Fryslan

Interactive Streamlit application for exploring historical BAG building demolition patterns in Fryslan (2011–2025).

## Requirements

- Windows 10/11
- Docker Desktop
- Access to a compatible PostgreSQL/PostGIS database

## Quick start

1. Download this repository using GitHub's Code > Download ZIP option.
2. Extract the ZIP file.
3. Copy `secrets.example.toml` into a new `.streamlit` folder.
4. Rename the copy to `secrets.toml`.
5. Enter valid PostgreSQL credentials.
6. Start Docker Desktop.
7. Double-click `Launch_Demolition.bat`.
8. Open http://localhost:8504

The launcher builds the Docker image and runs Streamlit.

## Database

The application requires PostgreSQL/PostGIS and the thesis-specific database objects used by its analytical queries.

When PostgreSQL runs on the same Windows computer as Docker, `host.docker.internal` can be used as the database hostname.

For databases hosted elsewhere, use the appropriate reachable hostname.

## Notes

- Database credentials must never be committed to GitHub.
- The complete PostgreSQL database is not distributed with this repository.
- Some 3D BAG functionality requires additional spatial datasets.
- Close the launcher or press Ctrl+C to stop Streamlit.

## Research

MSc GIMA thesis — historical building demolition patterns in Fryslan, 2011–2025.
