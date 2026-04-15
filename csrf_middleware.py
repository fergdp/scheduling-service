import os
import secrets
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from fastapi import HTTPException

# Detect environment
APP_ENVIRONMENT = os.getenv("APP_ENVIRONMENT", "development").lower()
IS_PRODUCTION = APP_ENVIRONMENT == "production"

CSRF_TOKEN_LENGTH = 32
CSRF_COOKIE_NAME = "XSRF-TOKEN"
CSRF_HEADER_NAME = "X-XSRF-TOKEN"

CSRF_COOKIE_CONFIG = {
    "httponly": False,
    "secure": IS_PRODUCTION,
    "samesite": "strict" if IS_PRODUCTION else "lax",
    "max_age": 3600,
    "path": "/",
}

CSRF_EXEMPT_ROUTES = ["/", "/docs", "/redoc", "/openapi.json", "/health"]
CSRF_PROTECTED_METHODS = ["POST", "PUT", "DELETE", "PATCH"]

def generate_csrf_token() -> str:
    return secrets.token_urlsafe(CSRF_TOKEN_LENGTH)

class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method.upper()

        if any(path.startswith(route) for route in CSRF_EXEMPT_ROUTES) or path == "/":
            response = await call_next(request)
            response.set_cookie(key=CSRF_COOKIE_NAME, value=generate_csrf_token(), **CSRF_COOKIE_CONFIG)
            return response

        csrf_cookie_token = request.cookies.get(CSRF_COOKIE_NAME)

        if method in CSRF_PROTECTED_METHODS:
            csrf_header_token = request.headers.get(CSRF_HEADER_NAME)
            if not csrf_cookie_token or not csrf_header_token or csrf_cookie_token != csrf_header_token:
                raise HTTPException(status_code=403, detail="CSRF token validation failed")

        response = await call_next(request)
        csrf_token = csrf_cookie_token if csrf_cookie_token else generate_csrf_token()
        response.set_cookie(key=CSRF_COOKIE_NAME, value=csrf_token, **CSRF_COOKIE_CONFIG)
        return response
