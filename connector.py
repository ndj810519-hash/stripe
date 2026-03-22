from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import os
import uuid
import json
import requests
import stripe
import firebase_admin

from firebase_admin import credentials, firestore
from datetime import datetime

app = FastAPI()

# ================= CORS =================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================= ENV =================

VOICEFLOW_API_KEY = os.getenv("VOICEFLOW_API_KEY")
VOICEFLOW_PROJECT_ID = os.getenv("VOICEFLOW_PROJECT_ID")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

stripe.api_key = STRIPE_SECRET_KEY

# ================= PRICE MAP =================

PRICE_MAP = {
    "ruslan": "price_1TDP3QLFX8j1bLMXM4V2iPwU",
    "seidkona": "price_1TDP1qLFX8j1bLMXPBe5DepK"
}

# ================= SUCCESS URL MAP =================

SUCCESS_URLS = {
    "ruslan": "https://enoma.kz/rus-chat",
    "seidkona": "https://enoma.kz/seid-chat"
}

# ================= FIREBASE =================

if not firebase_admin._apps:
    firebase_json = os.getenv("FIREBASE_KEY_JSON")
    cred = credentials.Certificate(json.loads(firebase_json))
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ================= VOICEFLOW =================

class UserMessage(BaseModel):
    message: str
    user_id: str | None = None
    app: str | None = None


@app.post("/ask")
def ask_voiceflow(data: UserMessage):

    user_id = data.user_id or str(uuid.uuid4())
    app_name = data.app

    if not app_name:
        return {"text": "App not specified"}

    user_ref = db.collection("users").document(user_id)
    user_doc = user_ref.get()

    if not user_doc.exists:
        raise HTTPException(status_code=403, detail="User not found")

    user_data = user_doc.to_dict()

    # 🔒 ПРОВЕРКА ДОСТУПА
    if not user_data.get("access", {}).get(app_name):
        return {
            "expired": True,
            "text": f"⛔ Нет доступа к {app_name}. Оплатите подписку."
        }

    # === Voiceflow ===

    url = f"https://general-runtime.voiceflow.com/state/user/{user_id}/interact"

    response = requests.post(
        url,
        headers={
            "Authorization": VOICEFLOW_API_KEY,
            "Content-Type": "application/json"
        },
        json={
            "request": {
                "type": "text",
                "payload": data.message
            }
        },
        params={"projectID": VOICEFLOW_PROJECT_ID}
    )

    traces = response.json()

    texts = [
        t["payload"]["message"]
        for t in traces if t.get("type") == "text"
    ]

    return {"text": "\n".join(texts)}

# ================= CREATE SUBSCRIPTION =================

@app.get("/create-subscription")
async def create_subscription(request: Request):

    email = request.query_params.get("email")
    uid = request.query_params.get("uid")
    app_name = request.query_params.get("app")

    if not email or not uid or not app_name:
        raise HTTPException(status_code=400, detail="Missing params")

    if app_name not in PRICE_MAP:
        raise HTTPException(status_code=400, detail="Invalid app")

    price_id = PRICE_MAP[app_name]
    success_url = SUCCESS_URLS.get(app_name)

    session = stripe.checkout.Session.create(
        mode="subscription",
        payment_method_types=["card"],
        customer_email=email,
        client_reference_id=uid,
        metadata={
            "app": app_name
        },
        line_items=[{
            "price": price_id,
            "quantity": 1,
        }],
        success_url=success_url,
        cancel_url="https://enoma.kz/"
    )

    return RedirectResponse(session.url)

# ================= STRIPE WEBHOOK =================

@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload,
            sig_header,
            STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # ✅ УСПЕШНАЯ ПОДПИСКА
    if event["type"] == "checkout.session.completed":

        session = event["data"]["object"]
        uid = session.get("client_reference_id")
        app_name = session["metadata"].get("app")

        if uid and app_name:
            db.collection("users").document(uid).set({
                f"access.{app_name}": True,
                "updatedAt": datetime.utcnow()
            }, merge=True)

    return {"status": "ok"}

# ================= CHECK ACCESS =================

@app.get("/check-access")
def check_access(uid: str, app: str):

    user_ref = db.collection("users").document(uid)
    user_doc = user_ref.get()

    if not user_doc.exists:
        return {"access": False}

    data = user_doc.to_dict()

    return {
        "access": data.get("access", {}).get(app, False)
    }

# ================= STATIC =================

from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory=".", html=True), name="static")
