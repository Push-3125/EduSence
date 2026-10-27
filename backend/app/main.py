from fastapi import FastAPI

from app.api import health

app = FastAPI(title="EduSense API")
app.include_router(health.router)
