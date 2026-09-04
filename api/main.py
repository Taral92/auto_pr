from contextlib import asynccontextmanager

from fastapi import FastAPI

from storage.db import close_pool, init_db

from .routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield
    close_pool()


app = FastAPI(title="auto-pr", lifespan=lifespan)
app.include_router(router)
