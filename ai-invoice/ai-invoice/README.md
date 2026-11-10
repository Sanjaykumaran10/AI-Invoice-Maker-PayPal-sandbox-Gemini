# AI Invoice Maker

Type rough notes about a job. AI turns them into a clean invoice, and the app creates a PayPal payment link. Built for the PayPal AI Hackathon with the PayPal Orders API (sandbox) and Claude.

## Run it
1. `python -m venv .venv && source .venv/bin/activate` (Windows: `.venv\Scripts\activate`)
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in your sandbox Client ID and Secret (developer.paypal.com, Apps & Credentials, Sandbox tab) and your Anthropic API key.
4. `python app.py`, then open http://localhost:5000

## Test a payment
1. Enter notes, click **Create invoice**, then **Create payment link**.
2. Open the link and sign in with your sandbox **Personal** (buyer) account.
3. After approval you return to `/success`, where the payment is captured.
4. Sign in to sandbox.paypal.com as the **Business** account to see the money arrive.

## How it works
`/api/parse` sends the notes to the AI, which returns JSON. The server re-validates it and recomputes all totals itself. `/api/create-order` creates a PayPal order. `/success` captures it.
