import logging
import os
import base64
from datetime import datetime
from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from jose import jwt, JWTError
from dotenv import load_dotenv
from pythonjsonlogger import jsonlogger

load_dotenv()

class CustomJsonFormatter(jsonlogger.JsonFormatter):
    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        log_record["timestamp"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        log_record["level"] = record.levelname
        log_record["service"] = "scheduling-service"
        log_record["environment"] = os.getenv("APP_ENVIRONMENT", "prod")
        log_record.setdefault("trace_id", "")
        log_record.setdefault("span_id", "")
        log_record.setdefault("user_id", "")
        log_record.setdefault("operation", "")
        log_record.setdefault("duration_ms", "")
        log_record.pop("color_message", None)

# Configure JSON logging
_json_formatter = CustomJsonFormatter("%(timestamp)s %(level)s %(name)s %(message)s")

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_json_formatter)

_handlers: list[logging.Handler] = [_stream_handler]

# Loki handler — only enabled when LOKI_URL env var is set
_loki_url = os.getenv("LOKI_URL")
if _loki_url:
    try:
        import logging_loki
        _loki_handler = logging_loki.LokiHandler(
            url=f"{_loki_url}/loki/api/v1/push",
            tags={"service": "scheduling-service", "level": "info"},
            version="1",
        )
        _loki_handler.setFormatter(_json_formatter)
        _handlers.append(_loki_handler)
    except Exception:
        pass  # Loki not available — logs still go to stdout

logging.root.handlers = _handlers
logging.root.setLevel(logging.INFO)

# Propagate gunicorn/uvicorn loggers to root
for _gunicorn_logger in ("gunicorn", "gunicorn.error", "gunicorn.access", "uvicorn"):
    _l = logging.getLogger(_gunicorn_logger)
    _l.handlers = []
    _l.propagate = True

# Load JWT Secret Key for middleware
SECRET_KEY_RAW = os.getenv("JWT_SECRET_KEY")
ALGORITHM = "HS256"

if not SECRET_KEY_RAW:
    raise ValueError("JWT_SECRET_KEY environment variable not set.")

try:
    SECRET_KEY_BYTES = base64.b64decode(SECRET_KEY_RAW)
except Exception as e:
    logging.error(f"Error decoding JWT secret key for middleware: {e}")
    raise ValueError("Invalid JWT_SECRET_KEY format for middleware.")

from csrf_middleware import CSRFMiddleware
from dependencies import get_clinic_id, get_user_id

# Setup Rate Limiter
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(
    title="Dental Clinic Scheduling Service",
    description="Microservice for handling multi-tenant dental appointments and Google Calendar sync.",
    version=os.getenv("APP_VERSION", "1.0.0"),
    docs_url="/docs",
    redoc_url="/redoc",
    redirect_slashes=False
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS Configuration
origins = [
    "http://localhost:3000",
    "https://atuconsul.com",
    "https://www.atuconsul.com"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "X-Requested-With",
        "X-XSRF-TOKEN"
    ],
)

# CSRF Middleware
app.add_middleware(CSRFMiddleware)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    user_id = "anonymous"
    clinic_id = "N/A"
    token = None

    # Priority 1: Try httpOnly cookie first
    token_cookie = request.cookies.get("token")
    if token_cookie:
        token = token_cookie
    # Priority 2: Fallback to Authorization header
    else:
        auth_header = request.headers.get("authorization")
        if auth_header:
            try:
                scheme, header_token = auth_header.split()
                if scheme.lower() == "bearer":
                    token = header_token
            except ValueError:
                logging.warning("Middleware: Invalid Authorization header format")

    # Decode token if found
    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY_BYTES, algorithms=[ALGORITHM])
            user_id = payload.get("sub", "unknown")
            clinic_id = payload.get("clinic_id", "N/A")
        except JWTError as e:
            logging.warning(f"Middleware JWT processing error: {e}")
            user_id = "invalid_token"

    logging.info(f"Incoming request: {request.method} {request.url.path} from user: {user_id} (clinic: {clinic_id})")
    response = await call_next(request)
    return response

@app.exception_handler(Exception)
async def exception_handler(request: Request, exc: Exception):
    user_id = "anonymous"
    clinic_id = "N/A"
    token = request.cookies.get("token")
    
    if not token:
        auth_header = request.headers.get("authorization")
        if auth_header:
            try:
                scheme, header_token = auth_header.split()
                if scheme.lower() == "bearer":
                    token = header_token
            except ValueError:
                pass

    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY_BYTES, algorithms=[ALGORITHM])
            user_id = payload.get("sub", "unknown")
            clinic_id = payload.get("clinic_id", "N/A")
        except JWTError:
            user_id = "invalid_token"

    logging.exception(f"Unhandled exception for request: {request.method} {request.url.path} from user: {user_id} (clinic: {clinic_id})", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error"},
    )

@app.get("/")
@limiter.limit("5/minute")
async def root(request: Request):
    return {
        "service": "Scheduling Service",
        "status": "Healthy",
        "version": os.getenv("APP_VERSION", "1.0.0")
    }

# Router inclusion
from routers import oauth, appointments
app.include_router(oauth.router, prefix="/clinic-scheduling-api/v1/oauth", tags=["OAuth"])
app.include_router(appointments.router, prefix="/clinic-scheduling-api/v1/appointments", tags=["Appointments"])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
