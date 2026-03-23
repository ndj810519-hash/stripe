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
            "text": "⛔ Оплатите доступ"
        }

    if not expires_at:
        return {
            "expired": True,
            "text": "⛔ Нет активной сессии"
        }

    if hasattr(expires_at, "tzinfo") and expires_at.tzinfo is not None:
        expires_at = expires_at.replace(tzinfo=None)

    if datetime.utcnow() > expires_at:

        user_ref.update({
            "hasAccess": False,
        })

        return {
            "expired": True,
            "text": "⏳ Время истекло"
        }

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
        })

        return {"access": False}

    return {"access": True}


# ================= FORTE CREATE ORDER =================

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


# ================= FORTE VERIFY AFTER PAYMENT =================

@app.get("/forte-success")
async def forte_success(request: Request):

    try:
        order_id = request.query_params.get("ID") or request.query_params.get("id")

        if not order_id:
            return RedirectResponse("https://enoma.kz/main-ru")

        response = requests.get(
            f"{FORTE_API_URL}/order/{order_id}",
            auth=(FORTE_USERNAME, FORTE_PASSWORD)
        )

        result = response.json()
        order_status = result.get("order", {}).get("status")

        if order_status not in ["FullyPaid", "Approved", "Deposited"]:
            return RedirectResponse("https://enoma.kz/main-ru")

        order_doc = db.collection("forte_orders").document(order_id).get()

        if not order_doc.exists:
            return RedirectResponse("https://enoma.kz/main-ru")

        order_info = order_doc.to_dict()

        if order_info.get("isProcessed"):
            agent = order_info.get("agent")

            if agent == "seidkona":
                return RedirectResponse("https://enoma.kz/seid-chat")
            return RedirectResponse("https://enoma.kz/rus-chat")

        uid = order_info["uid"]
        agent = order_info.get("agent", "ruslan")

        now = datetime.utcnow()
        expires_at = now + timedelta(minutes=5)

        db.collection("users").document(uid).set({
            "hasAccess": True,
            "expiresAt": expires_at,
            "agent": agent,
            "lastPaymentAt": now
        }, merge=True)

        db.collection("forte_orders").document(order_id).update({
            "isProcessed": True,
            "paidAt": now
        })

        if agent == "seidkona":
            return RedirectResponse("https://enoma.kz/seid-chat")

        return RedirectResponse("https://enoma.kz/rus-chat")

    except Exception as e:
        return {"error": str(e)}
