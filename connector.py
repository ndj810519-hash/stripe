# === АНТИ-АБЬЮЗ ВЕРСИЯ ===

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import os
import uuid
import json
import requests
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

FORTE_API_URL = os.getenv("FORTE_API_URL")
FORTE_USERNAME = os.getenv("FORTE_USERNAME")
FORTE_PASSWORD = os.getenv("FORTE_PASSWORD")

# ================= FIREBASE =================

if not firebase_admin._apps:
    firebase_json = os.getenv("FIREBASE_KEY_JSON")
    cred = credentials.Certificate(json.loads(firebase_json))
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ================= MODEL =================

class UserMessage(BaseModel):
    message: str
    user_id: str
    agent: str  # 🔥 добавили контроль чата

# ================= ASK (ЖЁСТКАЯ ПРОВЕРКА) =================

@app.post("/ask")
def ask_voiceflow(data: UserMessage):

    user_ref = db.collection("users").document(data.user_id)
    user_doc = user_ref.get()

    if not user_doc.exists:
        return {"expired": True, "text": "⛔ Нет доступа"}

    user_data = user_doc.to_dict()

    has_access = user_data.get("hasAccess")
    expires_at = user_data.get("expiresAt")
    user_agent = user_data.get("agent")

    # ❌ нет доступа
    if not has_access:
        return {"expired": True, "text": "⛔ Оплатите доступ"}

    # ❌ не тот чат
    if user_agent != data.agent:
        return {"expired": True, "text": "⛔ Неверный доступ"}

    # ❌ нет времени
    if not expires_at:
        return {"expired": True, "text": "⛔ Сессия не активна"}

    if hasattr(expires_at, "tzinfo") and expires_at.tzinfo:
        expires_at = expires_at.replace(tzinfo=None)

    # ⛔ ВРЕМЯ ИСТЕКЛО
    if datetime.utcnow() > expires_at:

        user_ref.update({
            "hasAccess": False
        })

        return {"expired": True, "text": "⏳ Время сессии истекло"}

    # ================= VOICEFLOW =================

    url = f"https://general-runtime.voiceflow.com/state/user/{data.user_id}/interact"

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

    traces = response.json()

    texts = []

    for trace in traces:
        if trace.get("type") == "text":
            texts.append(trace["payload"]["message"])

    return {"text": "\n".join(texts)}

# ================= CREATE ORDER =================

@app.get("/create-forte-order")
async def create_forte_order(uid: str, agent: str):

    payload = {
        "order": {
            "typeRid": "Order_RID",
            "language": "ru",
            "amount": "990.00",
            "currency": "KZT",
            "description": f"{uid}|question|{agent}",
            "title": "Quick Question",
            "hppRedirectUrl": "https://stripe-2dya.onrender.com/forte-success"
        }
    }

    response = requests.post(
        f"{FORTE_API_URL}/order",
        json=payload,
        auth=(FORTE_USERNAME, FORTE_PASSWORD),
        headers={"Content-Type": "application/json"}
    )

    forte_response = response.json()

    order_id = str(forte_response["order"]["id"])
    order_password = forte_response["order"]["password"]
    hpp_url = forte_response["order"]["hppUrl"]

    db.collection("forte_orders").document(order_id).set({
        "uid": uid,
        "agent": agent,
        "createdAt": datetime.utcnow(),
        "isProcessed": False
    })

    pay_url = f"{hpp_url}?id={order_id}&password={order_password}"

    return RedirectResponse(pay_url)

# ================= SUCCESS =================

@app.get("/forte-success")
async def forte_success(request: Request):

    order_id = request.query_params.get("id") or request.query_params.get("ID")

    if not order_id:
        return RedirectResponse("https://enoma.kz/main-ru")

    response = requests.get(
        f"{FORTE_API_URL}/order/{order_id}",
        auth=(FORTE_USERNAME, FORTE_PASSWORD)
    )

    result = response.json()
    status = result.get("order", {}).get("status")

    if status not in ["FullyPaid", "Approved", "Deposited"]:
        return RedirectResponse("https://enoma.kz/main-ru")

    order_doc = db.collection("forte_orders").document(order_id).get()

    if not order_doc.exists:
        return RedirectResponse("https://enoma.kz/main-ru")

    order_data = order_doc.to_dict()

    uid = order_data["uid"]
    agent = order_data.get("agent", "ruslan")

    now = datetime.utcnow()

    # 🔥 5 МИНУТ ЖЁСТКО
    expires_at = now + timedelta(minutes=5)

    db.collection("users").document(uid).set({
        "hasAccess": True,
        "expiresAt": expires_at,
        "agent": agent,
        "lastPaymentAt": now
    }, merge=True)

    db.collection("forte_orders").document(order_id).update({
        "isProcessed": True
    })
@app.get("/session-time")
def session_time(uid: str):

    user_ref = db.collection("users").document(uid)
    user_doc = user_ref.get()

    if not user_doc.exists:
        return {"hasAccess": False, "remainingSeconds": 0}

    data = user_doc.to_dict()

    if not data.get("hasAccess"):
        return {"hasAccess": False, "remainingSeconds": 0}

    expires_at = data.get("expiresAt")

    if not expires_at:
        return {"hasAccess": False, "remainingSeconds": 0}

    if hasattr(expires_at, "tzinfo") and expires_at.tzinfo:
        expires_at = expires_at.replace(tzinfo=None)

    remaining = (expires_at - datetime.utcnow()).total_seconds()

    if remaining <= 0:
        user_ref.update({"hasAccess": False})
        return {"hasAccess": False, "remainingSeconds": 0}

    return {
        "hasAccess": True,
        "remainingSeconds": int(remaining)
    }
    if agent == "seidkona":
        return RedirectResponse("https://enoma.kz/seid-chat")

    return RedirectResponse("https://enoma.kz/rus-chat")
