from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import get_engine


@asynccontextmanager
async def lifespan(_: FastAPI):
    s = get_settings()
    setup_logging(s.log_level, json_logs=s.app_env == "prod")
    structlog.get_logger().info("startup", env=s.app_env)
    yield
    await get_engine().dispose()


app = FastAPI(title="Startup Intelligence Platform", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=get_settings().cors_list, allow_methods=["GET"], allow_headers=["*"]
)
app.include_router(health.router)
