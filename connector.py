from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import os
import json
import requests
import stripe
import firebase_admin

from firebase_admin import credentials, firestore

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

# 👉 ВСТАВЬ СВОЙ price_id
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID")

stripe.api_key = STRIPE_SECRET_KEY

# ================= FIREBASE =================

if not firebase_admin._apps:
    firebase_json = os.getenv("FIREBASE_KEY_JSON")
    cred = credentials.Certificate(json.loads(firebase_json))
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ================= VOICEFLOW =================

class UserMessage(BaseModel):
    message: str
    user_id: str


@app.post("/ask")
def ask_voiceflow(data: UserMessage):

    user_ref = db.collection("users").document(data.user_id)
    user_doc = user_ref.get()

    if not user_doc.exists:
        raise HTTPException(status_code=403, detail="User not found")

    user_data = user_doc.to_dict()

    # ❗ ПРОСТАЯ И ЖЁСТКАЯ ПРОВЕРКА
    if not user_data.get("hasAccess"):
        return {
            "expired": True,
            "text": "⛔ Подписка не активна. Оплатите доступ."
        }

    # ===== Voiceflow =====

    url = f"https://general-runtime.voiceflow.com/state/user/{data.user_id}/interact"

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

    texts = []
    for t in traces:
        if t.get("type") == "text":
            texts.append(t["payload"]["message"])

    return {"text": "\n".join(texts)}

# ================= CREATE SUBSCRIPTION =================

@app.get("/create-subscription")
async def create_subscription(request: Request):

    email = request.query_params.get("email")
    uid = request.query_params.get("uid")

    session = stripe.checkout.Session.create(
        mode="subscription",
        payment_method_types=["card"],
        customer_email=email,
        client_reference_id=uid,
        line_items=[{
            "price": STRIPE_PRICE_ID,
            "quantity": 1,
        }],
        success_url="https://chat-rus.carrd.co/",
        cancel_url="https://your-site.carrd.co/"
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

    # ✅ УСПЕШНАЯ ОПЛАТА
    if event["type"] == "checkout.session.completed":

        session = event["data"]["object"]
        uid = session.get("client_reference_id")

        db.collection("users").document(uid).set({
            "hasAccess": True,
            "subscriptionActive": True
        }, merge=True)

    # ❌ НЕ СПИСАЛИСЬ ДЕНЬГИ
    if event["type"] == "invoice.payment_failed":

        invoice = event["data"]["object"]
        customer_email = invoice.get("customer_email")

        users = db.collection("users").stream()

        for user in users:
            data = user.to_dict()
            if data.get("email") == customer_email:
                db.collection("users").document(user.id).update({
                    "hasAccess": False,
                    "subscriptionActive": False
                })

    # ❌ ПОДПИСКА ОТМЕНЕНА
    if event["type"] == "customer.subscription.deleted":

        subscription = event["data"]["object"]
        customer = subscription.get("customer")

        users = db.collection("users").stream()

        for user in users:
            db.collection("users").document(user.id).update({
                "hasAccess": False,
                "subscriptionActive": False
            })

    return {"status": "ok"}
