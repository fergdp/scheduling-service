import logging
import os
from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from .csrf_middleware import CSRFMiddleware
from .dependencies import get_clinic_id, get_user_id

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Setup Rate Limiter
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(
    title="Dental Clinic Scheduling Service",
    description="Microservice for handling multi-tenant dental appointments and Google Calendar sync.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS Configuration
origins = [
    "http://localhost:3000",
    "https://atuconsul.com",
    "http://atuconsul.com"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# CSRF Middleware
app.add_middleware(CSRFMiddleware)

@app.get("/")
@limiter.limit("5/minute")
async def root(request: Request):
    return {
        "service": "Scheduling Service",
        "status": "Healthy",
        "version": os.getenv("APP_VERSION", "1.0.0")
    }

# Router inclusion
from .routers import oauth, appointments
app.include_router(oauth.router, prefix="/clinic-scheduling-api/v1/oauth", tags=["OAuth"])
app.include_router(appointments.router, prefix="/clinic-scheduling-api/v1/appointments", tags=["Appointments"])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
