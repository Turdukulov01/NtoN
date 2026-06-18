from fastapi import FastAPI
from fastapi.responses import PlainTextResponse


app = FastAPI(
    title="ChainLens Docker API",
    version="1.0.0",
    description="Operational API shell for the available project snapshot.",
)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    return (
        "# HELP chainlens_up Whether the Docker API shell is running.\n"
        "# TYPE chainlens_up gauge\n"
        "chainlens_up 1\n"
    )
