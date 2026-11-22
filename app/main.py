"""FastAPI entrypoint.  Run:  uvicorn app.main:app --host 0.0.0.0 --port 4000"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import get_settings
from app.errors import AppError
from app.schemas import ErrorResponse, LinkAnalysisRequest, LinkAnalysisResponse
from app.services.analysis_service import AnalysisService
from app.services.competitor_service import CompetitorService
from app.services.llm_service import LLMService
from app.services.website_extractor import SafeFetcher, WebsiteExtractor
from app.utils.cache import TTLCache
from app.utils.ratelimit import RateLimiter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("link_analysis.api")


def build_service(settings, client: httpx.AsyncClient) -> AnalysisService:
    fetcher = SafeFetcher(settings=settings)
    return AnalysisService(
        settings=settings,
        extractor=WebsiteExtractor(fetcher, settings),
        llm=LLMService(settings, client),
        competitors=CompetitorService(fetcher, settings),
        cache=TTLCache(settings.cache_ttl_seconds, settings.cache_max_items),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    client = httpx.AsyncClient(limits=httpx.Limits(max_connections=50, max_keepalive_connections=10))
    app.state.service = build_service(settings, client)
    app.state.rate_limiter = RateLimiter(settings.rate_limit_per_minute, settings.trust_proxy_headers)
    primary = app.state.service.llm.primary_label
    log.info("startup: provider=%s model=%s configured=%s", primary[0], primary[1], bool(app.state.service.llm.chain))
    yield
    await client.aclose()


app = FastAPI(
    title="AI Website Link Analysis API",
    version="1.0.0",
    description=(
        "Analyses a website URL with an LLM and returns competitor comparison, top-5 target search queries, "
        "SEO improvements, up to 5 services and target audience as structured JSON."
    ),
    lifespan=lifespan,
)
_settings = get_settings()
app.add_middleware(
    CORSMiddleware, allow_origins=[o.strip() for o in (_settings.cors_origins or "*").split(",")],
    allow_methods=["POST", "GET"], allow_headers=["*"],
)


def get_service(request: Request) -> AnalysisService:
    return request.app.state.service


def rate_limit(request: Request) -> None:
    request.app.state.rate_limiter.check(request)


def _error(status: int, code: str, message: str, headers: dict | None = None, details=None) -> JSONResponse:
    body = {"success": False, "error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body, headers=headers)


@app.exception_handler(AppError)
async def handle_app_error(_: Request, exc: AppError):
    return _error(exc.status_code, exc.code, exc.message, exc.headers)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_: Request, exc: RequestValidationError):
    details = [{"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]} for e in exc.errors()]
    return _error(422, "invalid_request", "Request body must be JSON like {\"url\": \"https://example.com\"}.", details=details)


@app.exception_handler(Exception)
async def handle_unexpected(_: Request, exc: Exception):
    log.exception("unhandled error")
    return _error(500, "internal_error", "Unexpected server error.")


@app.middleware("http")
async def access_log(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    log.info("%s %s -> %d (%d ms)", request.method, request.url.path, response.status_code,
             int((time.perf_counter() - started) * 1000))
    return response


ERROR_RESPONSES = {code: {"model": ErrorResponse, "description": text} for code, text in {
    400: "URL blocked by SSRF protection", 415: "URL is not an HTML page",
    422: "Invalid URL / request, page not found or no readable content",
    429: "Rate limit exceeded", 502: "Website or LLM failure", 503: "LLM not configured / rate limited",
    504: "Website timed out"}.items()}


@app.post("/link-analysis", response_model=LinkAnalysisResponse, responses=ERROR_RESPONSES, tags=["analysis"],
          summary="Analyse a website URL")
async def link_analysis(payload: LinkAnalysisRequest, service: AnalysisService = Depends(get_service),
                        _: None = Depends(rate_limit)) -> LinkAnalysisResponse:
    return await service.analyze(payload.url)


@app.get("/health", tags=["meta"], summary="Liveness + configuration check")
async def health(service: AnalysisService = Depends(get_service)):
    provider, model = service.llm.primary_label
    return {"status": "ok", "provider": provider, "model": model, "llm_configured": bool(service.llm.chain)}


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/docs")
