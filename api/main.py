from contextlib import asynccontextmanager

from fastapi import FastAPI

from storage.db import connect, init_db

from .routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = connect()
    try:
        init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(title="auto-pr", lifespan=lifespan)
app.include_router(router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api.main:app", host="0.0.0.0", port=8000)
