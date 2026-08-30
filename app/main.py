import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import __version__, ring
from app.config import settings
from app.db import init_db
from app.routes import (
    audit,
    auth,
    collections,
    dashboard,
    devices,
    holidays,
    mp3s,
    ring_hook,
    schedules,
    settings_ui,
)
from app.scheduler import start as start_scheduler
from app.scheduler import stop as stop_scheduler
from app.templating import templates

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)
log = logging.getLogger("doorbird.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    ring.start()
    log.info("doorbird-seasonal v%s ready", __version__)
    try:
        yield
    finally:
        # Cancel first: a pending auto response can be waiting up to an hour,
        # and there is no point holding shutdown for a message nobody will hear.
        ring.cancel_pending_auto_responses()
        ring.stop()
        stop_scheduler()


app = FastAPI(
    title="DoorBird Seasonal Sounds",
    version=__version__,
    lifespan=lifespan,
    # Unauthenticated by nature, so they stay off unless explicitly enabled.
    docs_url="/docs" if settings.enable_api_docs else None,
    redoc_url="/redoc" if settings.enable_api_docs else None,
    openapi_url="/openapi.json" if settings.enable_api_docs else None,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    # `Secure` is driven by configuration rather than hardcoded off: the usual
    # deployment here is plain HTTP on a LAN, but anyone terminating TLS at a
    # reverse proxy needs the cookie marked, and used to have no way to say so.
    https_only=settings.session_https_only,
    same_site="lax",
    max_age=settings.session_max_age_seconds,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(devices.router)
app.include_router(schedules.router)
app.include_router(schedules.auto_response_router)
app.include_router(mp3s.router)
app.include_router(collections.router)
app.include_router(holidays.router)
app.include_router(audit.router)
app.include_router(settings_ui.router)
app.include_router(ring_hook.router)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__}


HEADINGS = {
    400: ("That didn't work", "warning"),
    401: ("Please log in", "warning"),
    403: ("Not allowed", "warning"),
    404: ("Not found", "secondary"),
    500: ("Something broke", "danger"),
}

HINTS = {
    400: "Check the values you entered and try again — nothing was saved.",
    404: "The page or item you asked for doesn't exist. It may have been deleted.",
    500: "This is a bug. The container logs (`docker compose logs`) will have the traceback.",
}


# Routes answered by machines rather than browsers. Named here once, next to
# the handlers that consult them, rather than as a prefix tuple that could
# drift from the routers -- the previous version listed "/api/", which no route
# in this app has ever served.
MACHINE_PATHS: tuple[str, ...] = (
    ring_hook.router.prefix + "/ring/",
    "/healthz",
)


def _wants_json(request: Request) -> bool:
    """Machine callers get JSON; browsers get a readable page.

    The ring webhook especially must never be answered with HTML or a redirect
    to the login page — that would turn a bad token into an apparent success
    and hide the misconfiguration.
    """
    if request.url.path.startswith(MACHINE_PATHS):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def _error_page(request: Request, status: int, detail: str):
    heading, variant = HEADINGS.get(status, ("Error", "danger"))
    back = request.headers.get("referer") or "/dashboard"
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "status": status,
            "heading": heading,
            "variant": variant,
            "detail": detail,
            "hint": HINTS.get(status),
            "back": back,
        },
        status_code=status,
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # 303s are how require_auth redirects to the login page; leave them alone.
    if exc.status_code in (301, 302, 303, 307, 308) and "Location" in (exc.headers or {}):
        return RedirectResponse(exc.headers["Location"], status_code=exc.status_code)
    if _wants_json(request):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    log.info("%s %s -> %s: %s", request.method, request.url.path, exc.status_code, exc.detail)
    return _error_page(request, exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    """Turn FastAPI's field-level JSON blob into one readable sentence."""
    if _wants_json(request):
        return JSONResponse({"error": exc.errors()}, status_code=422)
    problems = []
    for err in exc.errors():
        field = " → ".join(str(p) for p in err.get("loc", ()) if p != "body")
        problems.append(f"{field or 'input'}: {err.get('msg', 'invalid')}")
    return _error_page(request, 400, "; ".join(problems) or "Invalid input.")


@app.exception_handler(500)
async def server_error(request: Request, exc):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    if _wants_json(request):
        return JSONResponse({"error": "internal server error"}, status_code=500)
    return _error_page(request, 500, "An unexpected error occurred.")


@app.exception_handler(404)
async def not_found(request: Request, exc):
    if _wants_json(request):
        return JSONResponse({"error": "not found"}, status_code=404)
    return _error_page(request, 404, "That page doesn't exist.")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return RedirectResponse("/dashboard")
