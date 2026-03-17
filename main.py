from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.voiceflow_router import router as voiceflow_router
from app.payments_router import router as payments_router
from app.subscription_router import router as subscription_router

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(voiceflow_router)
app.include_router(payments_router)
app.include_router(subscription_router)

@app.get("/")
def root():
    return {"server": "running"}
