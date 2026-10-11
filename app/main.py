import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.services import calls as call_service
from app.web import browser_call_routes, fake_routes, routes, twilio_routes
from app.worker.dialer import run_forever

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("volna")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info(
        "Volna стартует: FAKE_CALLS=%s, live=%s, backend=%s",
        settings.fake_calls,
        settings.live_model,
        settings.backend_model,
    )
    stop = asyncio.Event()
    worker_task = asyncio.create_task(run_forever(stop)) if settings.worker_enabled else None
    try:
        yield
    finally:
        stop.set()
        if worker_task:
            try:
                await asyncio.wait_for(worker_task, timeout=10)
            except TimeoutError:
                worker_task.cancel()
        # разборы звонков идут фоновыми задачами: даём им дописать результат
        await call_service.wait_post_call_tasks(timeout=10)


def create_app() -> FastAPI:
    app = FastAPI(title="Volna", lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=get_settings().session_secret)
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "web" / "static")),
        name="static",
    )
    app.include_router(routes.router)
    app.include_router(twilio_routes.router)
    app.include_router(fake_routes.router)
    app.include_router(browser_call_routes.router)

    @app.get("/health")
    async def health():
        return {"status": "ok", "fake_calls": get_settings().fake_calls}

    return app


app = create_app()
