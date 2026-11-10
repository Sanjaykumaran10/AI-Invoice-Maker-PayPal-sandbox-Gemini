"""AI Invoice Maker: messy text -> clean invoice -> PayPal payment link."""
import json
import os
import re
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests
from anthropic import Anthropic
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, render_template_string, request

load_dotenv()

PAYPAL_BASE = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")
CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "")
APP_URL = os.getenv("APP_URL", "http://localhost:5000")
MODEL = os.getenv("AI_MODEL", "claude-sonnet-5-5")
CURRENCIES = {"USD", "EUR", "GBP", "CAD", "AUD"}

app = Flask(__name__)

SYSTEM_PROMPT = """You turn a freelancer's rough notes into invoice data.
Reply with ONLY a JSON object, no markdown, in this shape:
{"client_name": string, "currency": "USD", "notes": string,
 "items": [{"description": string, "quantity": number, "unit_price": number}]}
Rules: quantity and unit_price are plain numbers. If the notes give a total for
an item, use quantity 1. Default currency is USD. Never invent items."""


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def normalize(raw: dict) -> dict:
    """Validate AI output and recompute every total ourselves (never trust the model's math)."""
    currency = str(raw.get("currency", "USD")).upper()
    if currency not in CURRENCIES:
        currency = "USD"
    items, total = [], Decimal("0")
    for it in raw.get("items", []):
        try:
            qty, price = Decimal(str(it["quantity"])), Decimal(str(it["unit_price"]))
        except (KeyError, InvalidOperation):
            continue
        if qty <= 0 or price < 0:
            continue
        line = money(qty * price)
        total += line
        items.append({
            "description": str(it.get("description", "Item"))[:120],
            "quantity": float(qty),
            "unit_price": str(money(price)),
            "line_total": str(line),
        })
    if not items:
        raise ValueError("No billable items found. Add quantities and prices to your notes.")
    return {
        "client_name": str(raw.get("client_name") or "Client")[:80],
        "currency": currency,
        "notes": str(raw.get("notes", ""))[:300],
        "items": items,
        "total": str(money(total)),
    }


def ai_parse(text: str) -> dict:
    client = Anthropic()  # reads ANTHROPIC_API_KEY
    msg = client.messages.create(
        model=MODEL,
        max_tokens=800,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    out = "".join(b.text for b in msg.content if b.type == "text")
    out = re.sub(r"^```(?:json)?|```$", "", out.strip(), flags=re.M).strip()
    return normalize(json.loads(out))


def paypal_token() -> str:
    r = requests.post(
        f"{PAYPAL_BASE}/v1/oauth2/token",
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/parse")
def api_parse():
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify(error="Write a few lines about the work first."), 400
    try:
        return jsonify(ai_parse(text))
    except ValueError as e:
        return jsonify(error=str(e)), 422
    except Exception as e:  # bad JSON from model, missing key, network...
        return jsonify(error=f"Could not read the notes: {e}"), 502


@app.post("/api/create-order")
def api_create_order():
    try:
        invoice = normalize(request.get_json(silent=True) or {})  # re-validate on the server
        invoice_no = "INV-" + uuid.uuid4().hex[:6].upper()
        headers = {
            "Authorization": f"Bearer {paypal_token()}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": str(uuid.uuid4()),
        }
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": invoice_no,
                "invoice_id": invoice_no,
                "description": f"Invoice for {invoice['client_name']}"[:127],
                "amount": {"currency_code": invoice["currency"], "value": invoice["total"]},
            }],
            "payment_source": {"paypal": {"experience_context": {
                "return_url": f"{APP_URL}/success",
                "cancel_url": f"{APP_URL}/",
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
            }}},
        }
        r = requests.post(f"{PAYPAL_BASE}/v2/checkout/orders", headers=headers, json=body, timeout=20)
        data = r.json()
        if not r.ok:
            return jsonify(error=data.get("details", [{}])[0].get("description", data.get("message", "PayPal error"))), 502
        link = next(l["href"] for l in data["links"] if l["rel"] in ("payer-action", "approve"))
        return jsonify(invoice_no=invoice_no, order_id=data["id"], pay_url=link)
    except ValueError as e:
        return jsonify(error=str(e)), 422
    except Exception as e:
        return jsonify(error=f"Could not create the payment link: {e}"), 502


SUCCESS_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Payment {{ status }}</title>
<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem">
<h1>{{ 'Payment received' if ok else 'Payment not completed' }}</h1>
{% if ok %}<p>{{ amount }} {{ currency }} paid. PayPal capture ID: <code>{{ capture_id }}</code></p>
<p>Check the Business account at sandbox.paypal.com to see the money arrive.</p>
{% else %}<p>{{ message }}</p>{% endif %}
<p><a href="/">Make another invoice</a></p></body>"""


@app.get("/success")
def success():
    """PayPal sends the buyer back here with ?token=<order id>. We capture the money now."""
    order_id = request.args.get("token", "")
    try:
        r = requests.post(
            f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}/capture",
            headers={"Authorization": f"Bearer {paypal_token()}", "Content-Type": "application/json"},
            timeout=20,
        )
        data = r.json()
        cap = data["purchase_units"][0]["payments"]["captures"][0]
        return render_template_string(
            SUCCESS_PAGE, ok=data.get("status") == "COMPLETED", status=data.get("status"),
            amount=cap["amount"]["value"], currency=cap["amount"]["currency_code"],
            capture_id=cap["id"], message="",
        )
    except Exception as e:
        return render_template_string(SUCCESS_PAGE, ok=False, status="failed", message=f"Capture failed: {e}")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
