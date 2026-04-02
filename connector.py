from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse
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
        return {"expired": True}

    user_data = user_doc.to_dict()

    if not user_data.get("hasAccess"):
        return {"expired": True}

    expires_at = user_data.get("expiresAt")

    if not expires_at:
        return {"expired": True}

    if hasattr(expires_at, "tzinfo") and expires_at.tzinfo:
        expires_at = expires_at.replace(tzinfo=None)

    if datetime.utcnow() > expires_at:
        user_ref.update({"hasAccess": False})
        return {"expired": True}

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

# ================= CREATE ORDER =================
@app.get("/create-forte-order")
async def create_forte_order(uid: str):

    payload = {
        "order": {
            "typeRid": "Order_RID",
            "language": "ru",
            "amount": "100.00",
            "currency": "KZT",
            "description": f"{uid}|5min",
            "title": "5-minute session",
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
    password = forte_response["order"]["password"]
    hpp_url = forte_response["order"]["hppUrl"]

    db.collection("forte_orders").document(order_id).set({
        "uid": uid,
        "createdAt": datetime.utcnow(),
        "isProcessed": False
    })

    return RedirectResponse(f"{hpp_url}?id={order_id}&password={password}")

# ================= SUCCESS =================
@app.get("/forte-success")
async def forte_success(request: Request):

    order_id = request.query_params.get("ID") or request.query_params.get("id")

    if not order_id:
        return RedirectResponse("https://enoma.kz")

    response = requests.get(
        f"{FORTE_API_URL}/order/{order_id}",
        auth=(FORTE_USERNAME, FORTE_PASSWORD)
    )

    result = response.json()
    status = result.get("order", {}).get("status")

    if status not in ["FullyPaid", "Approved", "Deposited"]:
        return RedirectResponse("https://enoma.kz")

    order_ref = db.collection("forte_orders").document(order_id)
    order_doc = order_ref.get()

    if not order_doc.exists:
        return RedirectResponse("https://enoma.kz")

    order_data = order_doc.to_dict()

    if order_data.get("isProcessed"):
        uid = order_data["uid"]
        return RedirectResponse(f"https://enoma.kz/rus-chat?uid={uid}")

    uid = order_data["uid"]

    now = datetime.utcnow()
    expires_at = now + timedelta(minutes=5)

    db.collection("users").document(uid).set({
        "hasAccess": True,
        "expiresAt": expires_at,
        "lastPaymentAt": now
    }, merge=True)

    order_ref.update({
        "isProcessed": True,
        "paidAt": now
    })

    return RedirectResponse(f"http://enoma.kz/rus-chat?uid={uid}&paid=1")

# ================= STATUS =================
@app.get("/subscription-status")
def subscription_status(uid: str):

    user_ref = db.collection("users").document(uid)
    user_doc = user_ref.get()

    if not user_doc.exists:
        return {"hasAccess": False, "remainingSeconds": 0}

    data = user_doc.to_dict()
    expires_at = data.get("expiresAt")

    if not expires_at:
        return {"hasAccess": False, "remainingSeconds": 0}

    if hasattr(expires_at, "tzinfo") and expires_at.tzinfo:
        expires_at = expires_at.replace(tzinfo=None)

    now = datetime.utcnow()
    remaining = int((expires_at - now).total_seconds())

    if remaining <= 0:
        return {"hasAccess": False, "remainingSeconds": 0}

    return {
        "hasAccess": True,
        "remainingSeconds": remaining
    }

# ================= STATIC =================
@app.get("/manifest.json")
def manifest():
    return FileResponse("manifest.json")

@app.get("/icon-192.png")
def icon_192():
    return FileResponse("icon-192.png")

@app.get("/icon-512.png")
def icon_512():
    return FileResponse("icon-512.png")
