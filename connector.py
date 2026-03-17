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
import requests
import base64
import os
import uuid

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

# ================= ENV VARIABLES =================

VOICEFLOW_API_KEY = os.getenv("VOICEFLOW_API_KEY")
VOICEFLOW_PROJECT_ID = os.getenv("VOICEFLOW_PROJECT_ID")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

FORTE_API_URL = os.getenv("FORTE_API_URL")
FORTE_USERNAME = os.getenv("FORTE_USERNAME")
FORTE_PASSWORD = os.getenv("FORTE_PASSWORD")

stripe.api_key = STRIPE_SECRET_KEY

# ================= FIREBASE INIT =================

if not firebase_admin._apps:
    firebase_json = os.getenv("FIREBASE_KEY_JSON")
    cred = credentials.Certificate(json.loads(firebase_json))
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ================= VOICEFLOW =================

class UserMessage(BaseModel):
    message: str
    user_id: str | None = None


@app.post("/ask")
def ask_voiceflow(data: UserMessage):

    user_id = data.user_id or str(uuid.uuid4())

    # ===== Проверяем пользователя =====

    user_ref = db.collection("users").document(user_id)
    user_doc = user_ref.get()

    if not user_doc.exists:
        raise HTTPException(status_code=403, detail="User not found")

    user_data = user_doc.to_dict()

    has_access = user_data.get("hasAccess")
    expires_at = user_data.get("expiresAt")

    # ===== Если нет доступа =====

    if not has_access:
        return {
            "expired": True,
            "text": "⛔ Your session has ended. Please purchase a new consultation."
        }

    # ===== Если нет времени окончания =====

    if not expires_at:
        return {
            "expired": True,
            "text": "⛔ No active subscription."
        }

    # ===== Убираем timezone если есть =====

    if hasattr(expires_at, "tzinfo") and expires_at.tzinfo is not None:
        expires_at = expires_at.replace(tzinfo=None)

    # ===== Проверяем время =====

    if datetime.utcnow() > expires_at:

        user_ref.update({
            "hasAccess": False,
            "minutesRemaining": 0
        })

        return {
            "expired": True,
            "text": "⏳ Your consultation time has expired. Please purchase a new session."
        }

    # ===== Если всё ок — обращаемся к Voiceflow =====

    url = f"https://general-runtime.voiceflow.com/state/user/{user_id}/interact"

    headers = {
        "Authorization": VOICEFLOW_API_KEY,
        "Content-Type": "application/json"
    }

    payload = {
        "request": {
            "type": "text",
            "payload": data.message
        },
        "config": {
            "tts": False,
            "stripSSML": True
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

    return {
        "text": "\n".join(texts)
    }
# ================= STRIPE CHECKOUT (SEIDKONA) =================

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
                "product_data": {"name": "Zodiac Wisdom"},
                "unit_amount": 999,
            },
            "quantity": 1,
        }],
        success_url="https://seid-chat.carrd.co",
        cancel_url="https://seidkona.carrd.co/",
    )

    return RedirectResponse(session.url)


# ================= STRIPE CHECKOUT (RUSLAN) =================

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
                "product_data": {"name": "Consultation with Ruslan"},
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

        if not uid:
            return {"status": "no user id"}

        metadata = session.get("metadata", {})
        agent = metadata.get("agent")

        user_ref = db.collection("users").document(uid)

        # Seidkona → 10 минут
        if agent == "seidkona":
            expires_at = datetime.utcnow() + timedelta(minutes=10)
            minutes_remaining = 10

        # Ruslan → 1 час
        elif agent == "ruslan":
            expires_at = datetime.utcnow() + timedelta(hours=1)
            minutes_remaining = 60

        else:
            expires_at = datetime.utcnow() + timedelta(minutes=10)
            minutes_remaining = 10

        user_ref.set({
            "minutesRemaining": minutes_remaining,
            "hasAccess": True,
            "expiresAt": expires_at
        }, merge=True)

        return {"status": "success"}

    return {"status": "ignored"}


# ================= CHECK ACCESS =================

@app.get("/check-access")
async def check_access(uid: str):

    user_ref = db.collection("users").document(uid)
    user = user_ref.get()

    if not user.exists:
        return {"access": False}

    data = user.to_dict()

    if not data.get("hasAccess"):
        return {"access": False}

    expires_at = data.get("expiresAt")

    if not expires_at:
        return {"access": False}

    if datetime.utcnow() > expires_at:

        user_ref.update({
            "hasAccess": False,
            "minutesRemaining": 0
        })

        return {"access": False}

    return {"access": True}

# ================= FORTE CREATE ORDER =================

@app.get("/create-forte-order")
async def create_forte_order(uid: str, plan: str, lang: str = "ru"):

    if not FORTE_API_URL or not FORTE_USERNAME or not FORTE_PASSWORD:
        raise HTTPException(status_code=500, detail="Forte credentials not configured")

    plan = plan.strip().lower()
    lang = lang.strip().lower()

    if lang not in ["ru", "en"]:
        lang = "ru"

    # ---- Тарифы ----
    if plan == "hour":
        amount = "9900.00"
    elif plan == "day":
        amount = "29990.00"
    elif plan == "month":
        amount = "89990.00"
    else:
        raise HTTPException(status_code=400, detail="Invalid plan")

    payload = {
        "order": {
            "typeRid": "Order_RID",
            "language": lang,
            "amount": amount,
            "currency": "KZT",
            "description": f"{uid}|{plan}|{lang}",
            "title": "Subscription",
            "hppRedirectUrl": "https://ei-brain.onrender.com/forte-success"
        }
    }

    response = requests.post(
        f"{FORTE_API_URL}/order",
        json=payload,
        auth=(FORTE_USERNAME, FORTE_PASSWORD),
        headers={"Content-Type": "application/json"}
    )

    response.raise_for_status()

    forte_response = response.json()

    order_id = str(forte_response["order"]["id"])
    order_password = forte_response["order"]["password"]
    hpp_url = forte_response["order"]["hppUrl"]

    # ---- Сохраняем заказ ДО оплаты ----
    db.collection("forte_orders").document(order_id).set({
        "uid": uid,
        "plan": plan,
        "lang": lang,
        "createdAt": datetime.utcnow(),
        "isProcessed": False
    })

    pay_url = f"{hpp_url}?id={order_id}&password={order_password}"

    return RedirectResponse(pay_url)



# ================= FORTE VERIFY AFTER PAYMENT =================

@app.get("/forte-success")
async def forte_success(request: Request):

    try:
        order_id = request.query_params.get("ID") or request.query_params.get("id")

        if not order_id:
            return {"error": "No order id received from Forte"}

        # ---- Проверяем заказ в Forte ----
        response = requests.get(
            f"{FORTE_API_URL}/order/{order_id}",
            auth=(FORTE_USERNAME, FORTE_PASSWORD)
        )

        result = response.json()
        order_status = result.get("order", {}).get("status")

        if order_status not in ["FullyPaid", "Approved", "Deposited"]:
            return RedirectResponse("https://gna-ei.kz/payment-failed")

        # ---- Получаем заказ из Firestore ----
        order_doc = db.collection("forte_orders").document(order_id).get()

        if not order_doc.exists:
            return RedirectResponse("https://gna-ei.kz/payment-failed")

        order_info = order_doc.to_dict()

        # ---- Защита от повторной обработки ----
        if order_info.get("isProcessed"):
            if order_info.get("lang") == "en":
                return RedirectResponse("https://gna-ei.kz/nj-assistant-en")
            return RedirectResponse("https://gna-ei.kz/nj-assistant")

        uid = order_info["uid"]
        plan = order_info["plan"]
        lang = order_info["lang"]

        now = datetime.utcnow()

        if plan == "hour":
            duration = timedelta(hours=1)
        elif plan == "day":
            duration = timedelta(days=1)
        elif plan == "month":
            duration = timedelta(days=30)

        expires_at = (now + duration).replace(microsecond=0)

        db.collection("users").document(uid).set({
            "hasAccess": True,
            "isPaid": True,
            "planType": plan,
            "expiresAt": expires_at,
            "expiresAtFormatted": expires_at.strftime("%d.%m.%Y %H:%M:%S"),
            "lastPaymentAt": now
        }, merge=True)

        # ---- Определяем срок подписки ----
        if plan == "hour":
            duration = timedelta(hours=1)
        elif plan == "day":
            duration = timedelta(days=1)
        elif plan == "month":
            duration = timedelta(days=30)
        else:
            return {"error": "Invalid plan"}

        expires_at = now + duration

        # ---- Обновляем пользователя ----
        db.collection("users").document(uid).set({
            "hasAccess": True,
            "isPaid": True,
            "planType": plan,
            "expiresAt": expires_at,
            "lastPaymentAt": now
        }, merge=True)

        # ---- Помечаем заказ как обработанный ----
        db.collection("forte_orders").document(order_id).update({
            "isProcessed": True,
            "paidAt": now
        })

        # ---- Записываем платеж ----
        db.collection("payments").document(order_id).set({
            "uid": uid,
            "plan": plan,
            "status": order_status,
            "orderId": order_id,
            "createdAt": now
        })

        # ---- Редирект по языку ----
        if lang == "en":
            return RedirectResponse("https://gna-ei.kz/online-session-en")

        return RedirectResponse("https://gna-ei.kz/online-session")

    except Exception as e:
        return {"error": str(e)}

    # ================= SUBSCRIPTION STATUS =================

@app.get("/subscription-status")
def subscription_status(uid: str):
    try:
        user_ref = db.collection("users").document(uid)
        user_doc = user_ref.get()

        if not user_doc.exists:
            return {"hasAccess": False, "remainingSeconds": 0}

        user_data = user_doc.to_dict()
        expires_at = user_data.get("expiresAt")

        if not expires_at:
            return {"hasAccess": False, "remainingSeconds": 0}

        # Убираем timezone если есть
        if hasattr(expires_at, "tzinfo") and expires_at.tzinfo is not None:
            expires_at = expires_at.replace(tzinfo=None)

        now = datetime.utcnow()
        remaining_seconds = int((expires_at - now).total_seconds())

        if remaining_seconds <= 0:
            return {"hasAccess": False, "remainingSeconds": 0}

        return {
            "hasAccess": True,
            "remainingSeconds": remaining_seconds,
            "expiresAt": expires_at
        }

    except Exception:
        return {"hasAccess": False, "remainingSeconds": 0}
