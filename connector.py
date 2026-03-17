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
from datetime import datetime, timedelta


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

    # ❗ ЖЁСТКО используем user_id (без UUID)
    user_id = data.user_id

    user_ref = db.collection("users").document(user_id)
    user_doc = user_ref.get()

    if not user_doc.exists:
        raise HTTPException(status_code=403, detail="User not found")

    user_data = user_doc.to_dict()

    has_access = user_data.get("hasAccess")
    expires_at = user_data.get("expiresAt")

    if not has_access:
        return {
            "expired": True,
            "text": "⛔ Session ended. Please оплатите доступ."
        }

    if not expires_at:
        return {
            "expired": True,
            "text": "⛔ Нет активной подписки."
        }

    if hasattr(expires_at, "tzinfo") and expires_at.tzinfo is not None:
        expires_at = expires_at.replace(tzinfo=None)

    if datetime.utcnow() > expires_at:

        user_ref.update({
            "hasAccess": False,
            "minutesRemaining": 0
        })

        return {
            "expired": True,
            "text": "⏳ Время сессии завершено. Оплатите новую консультацию."
        }

    # ===== Voiceflow =====

    url = f"https://general-runtime.voiceflow.com/state/user/{user_id}/interact"

    headers = {
        "Authorization": VOICEFLOW_API_KEY,
        "Content-Type": "application/json"
    }

    payload = {
        "request": {
            "type": "text",
            "payload": data.message
        }
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        params={"projectID": VOICEFLOW_PROJECT_ID}
    )

    if response.status_code != 200:
        return {"error": response.text}

    traces = response.json()

    texts = []
    for trace in traces:
        if trace.get("type") == "text":
            texts.append(trace["payload"]["message"])

    return {"text": "\n".join(texts)}


# ================= STRIPE CHECKOUT =================

@app.get("/create-checkout-session")
async def create_checkout_session(request: Request):

    email = request.query_params.get("email")
    uid = request.query_params.get("uid")

    session = stripe.checkout.Session.create(
        client_reference_id=uid,
        payment_method_types=["card"],
        mode="payment",
        customer_email=email,
        metadata={
            "user_id": uid,
            "agent": "seidkona"
        },
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Consultation 10 min"},
                "unit_amount": 999,
            },
            "quantity": 1,
        }],
        success_url="https://seid-chat.carrd.co",
        cancel_url="https://seidkona.carrd.co/",
    )

    return RedirectResponse(session.url)


@app.get("/create-checkout-session-ruslan")
async def create_checkout_session_ruslan(request: Request):

    email = request.query_params.get("email")
    uid = request.query_params.get("uid")

    session = stripe.checkout.Session.create(
        client_reference_id=uid,
        payment_method_types=["card"],
        mode="payment",
        customer_email=email,
        metadata={
            "user_id": uid,
            "agent": "ruslan"
        },
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Consultation 1 hour"},
                "unit_amount": 999,
            },
            "quantity": 1,
        }],
        success_url="https://chat-rus.carrd.co/",
        cancel_url="https://ruslan-sp.carrd.co/",
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

    if event["type"] == "checkout.session.completed":

        session = event["data"]["object"]
        uid = session.get("client_reference_id")

        metadata = session.get("metadata", {})
        agent = metadata.get("agent")

        user_ref = db.collection("users").document(uid)

        # логика времени
        if agent == "ruslan":
            expires_at = datetime.utcnow() + timedelta(hours=1)
            minutes = 60
        else:
            expires_at = datetime.utcnow() + timedelta(minutes=10)
            minutes = 10

        user_ref.set({
            "hasAccess": True,
            "minutesRemaining": minutes,
            "expiresAt": expires_at
        }, merge=True)

        return {"status": "success"}

    return {"status": "ignored"}
