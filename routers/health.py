import asyncio
import logging
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session
from dependencies import get_db

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)

# Fail readiness if DB takes longer than this
DB_CHECK_TIMEOUT_SECONDS = 2.0


@router.get("/health/live", status_code=200)
async def liveness():
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness(response: Response, db: Session = Depends(get_db)):
    checks: dict[str, str] = {}
    healthy = True

    try:
        await asyncio.wait_for(
            asyncio.to_thread(lambda: db.execute(text("SELECT 1")).scalar()),
            timeout=DB_CHECK_TIMEOUT_SECONDS,
        )
        checks["database"] = "ok"
    except asyncio.TimeoutError:
        checks["database"] = "timeout"
        healthy = False
    except Exception as exc:
        # Google Calendar is intentionally excluded from readiness — local
        # appointments keep working when Google is unavailable.
        logger.warning(
            "readiness db check failed",
            extra={"operation": "health.ready", "error": str(exc)},
        )
        checks["database"] = "error"
        healthy = False

    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "checks": checks}
    return {"status": "ready", "checks": checks}


@router.get("/health")
async def health_alias(response: Response, db: Session = Depends(get_db)):
    return await readiness(response, db)
