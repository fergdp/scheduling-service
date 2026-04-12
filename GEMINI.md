# Gemini Project Context: scheduling-service

This microservice handles multi-tenant dental appointments, dentist availability, and deep integration with the **Google Calendar API v3**. It is built with **FastAPI** for high performance and asynchronous communication.

## Project Overview

- **Technology Stack:** Python 3.12+, FastAPI, SQLAlchemy 2.0, Alembic, Google API Client.
- **Database:** Independent MySQL Database (`dental_scheduling_db`).
- **Security:**
    - **JWT Verification:** Shared `JWT_SECRET_KEY` with the central `dental-clinic` service. Decodes HS256 tokens from httpOnly cookies.
    - **CSRF Protection:** Double-submit cookie pattern (`X-XSRF-TOKEN`).
    - **Token Encryption:** Google `refresh_tokens` are encrypted at rest using **Fernet (AES-128)** with a `FERNET_KEY`.
    - **Rate Limiting:** IP and user-based limits via `slowapi`.
- **Key Features:**
    - **OAuth2 Flow:** Securely link dentist Google accounts.
    - **Availability Engine:** Real-time calculation of free slots by merging Google `FreeBusy` with local `REQUESTED` appointments.
    - **Approval Workflow:** Appointments are local-only (`REQUESTED`) until approved by the dentist, then synced to Google Calendar (`APPROVED`).

## Building and Running

### Common Commands
- **Install Environment:**
  ```bash
  python -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt
  ```
- **Database Migrations:**
  ```bash
  alembic revision --autogenerate -m "Description"
  alembic upgrade head
  ```
- **Run Locally:**
  ```bash
  uvicorn main:app --port 8002 --reload
  ```

## Development Conventions

- **Multi-Tenancy:** Strictly enforce `clinic_id` isolation. (Future: Implement the automatic `SQL Guard` listener in `models.py`).
- **Timezones:** All appointment times are stored in **UTC**.
- **API Routing:** Prefixed with `/clinic-scheduling-api/v1/`.

## Directory Structure
- `routers/`: REST endpoints (OAuth, Appointments).
- `utils/`: Core logic (Google API, Cryptography).
- `alembic/`: Database migration scripts.
- `models.py`: SQLAlchemy table definitions.
- `schemas.py`: Pydantic validation models.
