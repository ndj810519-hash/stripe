from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import os
import uuid
import json
import requests
import firebase_admin
import base64

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

FORTE_API_URL = os.getenv("FORTE_API_URL")
FORTE_USERNAME = os.getenv("FORTE_USERNAME")
FORTE_PASSWORD = os.getenv("FORTE_PASSWORD")

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
            "text": "⛔ Ваш сеанс закончен"
        }

    # ===== Если нет времени окончания =====

    if not expires_at:
        return {
            "expired": True,
            "text": "⛔ Время закончилось"
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
            "text": "⏳ Осталось до завершения сеанса"
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
    if plan == "minute":
        amount = "100.00"
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
            "hppRedirectUrl": "https://stripe-2dya.onrender.com/forte-success"
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
            return RedirectResponse("https://enoma.kz/main-ru/payment-failed")

        # ---- Получаем заказ из Firestore ----
        order_doc = db.collection("forte_orders").document(order_id).get()

        if not order_doc.exists:
            return RedirectResponse("https://enoma.kz/main-ru/payment-failed")

        order_info = order_doc.to_dict()

        # ---- Защита от повторной обработки ----
        if order_info.get("isProcessed"):
            if order_info.get("lang") == "kz":
                return RedirectResponse("https://enoma.kz/rus-chat-kz")
            return RedirectResponse("https://enoma.kz/rus-chat")

        uid = order_info["uid"]
        plan = order_info["plan"]
        lang = order_info["lang"]

        now = datetime.utcnow()

        if plan == "minute":
            duration = timedelta(minute=5)

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
        if plan == "minute":
            duration = timedelta(minute=5)
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
        if lang == "kz":
            return RedirectResponse("http://enoma.kz/rus-chat-kz")

        return RedirectResponse("http://enoma.kz/rus-chat")

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


from fastapi.responses import FileResponse

@app.get("/manifest.json")
def manifest():
    return FileResponse("manifest.json")

@app.get("/icon-192.png")
def icon_192():
    return FileResponse("icon-192.png")

@app.get("/icon-512.png")
def icon_512():
    return FileResponse("icon-512.png")
