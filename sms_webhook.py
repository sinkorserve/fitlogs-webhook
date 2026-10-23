"""
fitlogs.ai -- Combined Webhook Server
Handles:
  1. Twilio inbound SMS -> parses fitness log, saves to Firestore, AI coach replies
     - Recognizes: food/meals, steps/movement, sleep, water, weight, workout, mood
     - COACH command: asks AI coach a question
     - SUMMARY command: get today's summary
     - HELP command: list commands
  2. Stripe webhook -> upgrades user plan on payment
  3. /insights endpoint -> Claude AI coach chat from dashboard
  4. /verify endpoints -> Twilio OTP for signup
  5. /account/update-phone -> migrate user data
  6. Daily/weekly email summaries

Install: pip install flask twilio firebase-admin requests gunicorn stripe anthropic

Environment variables:
    TWILIO_AUTH_TOKEN
    TWILIO_ACCOUNT_SID
    FIREBASE_CREDENTIALS_JSON
    STRIPE_SECRET_KEY
    STRIPE_WEBHOOK_SECRET
    ANTHROPIC_API_KEY
    TWILIO_VERIFY_SERVICE_SID
    ZOHO_EMAIL
    ZOHO_PASSWORD
    CRON_SECRET
"""

from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
from datetime import datetime, timezone, timedelta
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import threading
import os, re, json, uuid
import requests as http
import firebase_admin
from firebase_admin import credentials, firestore
import stripe
import anthropic

app = Flask(__name__)

# ── CORS ────────────────────────────────────────────────────────────────────
@app.after_request
def add_cors_headers(response):
    origin = request.headers.get('Origin', '')
    allowed = ['https://fitlogs.ai', 'https://www.fitlogs.ai',
               'https://fitlogs-git-dev-sinkorserves-projects.vercel.app']
    if origin in allowed or '.vercel.app' in origin or origin.endswith('fitlogs.ai'):
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        response.headers['Access-Control-Max-Age'] = '86400'
        if request.method == 'OPTIONS':
            response.status_code = 200
    return response

@app.route('/insights', methods=['OPTIONS'])
def insights_options():
    return '', 204

@app.route('/coach', methods=['OPTIONS'])
def coach_options():
    return '', 204

# ── Config ──────────────────────────────────────────────────────────────────
TWILIO_AUTH_TOKEN     = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_ACCOUNT_SID    = os.environ.get("TWILIO_ACCOUNT_SID", "")
VALIDATE_TWILIO       = os.environ.get("VALIDATE_TWILIO", "true").lower() == "true"
STRIPE_SECRET_KEY     = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
ANTHROPIC_API_KEY     = os.environ.get("ANTHROPIC_API_KEY", "")
TWILIO_NUMBER         = "+18885965467"
VERIFY_SERVICE_SID    = os.environ.get("TWILIO_VERIFY_SERVICE_SID", "")

stripe.api_key  = STRIPE_SECRET_KEY
claude_client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

ZOHO_EMAIL    = os.environ.get("ZOHO_EMAIL", "admin@fitlogs.ai")
ZOHO_PASSWORD = os.environ.get("ZOHO_PASSWORD", "")
SMTP_HOST     = "smtp.zoho.com"
SMTP_PORT     = 465
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ── Firebase ────────────────────────────────────────────────────────────────
if not firebase_admin._apps:
    creds_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    if creds_json:
        cred = credentials.Certificate(json.loads(creds_json))
    else:
        cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ── Parse fitness log from SMS ───────────────────────────────────────────────
FOOD_KEYWORDS    = ['ate', 'had', 'eaten', 'breakfast', 'lunch', 'dinner', 'snack',
                    'drank', 'drink', 'coffee', 'tea', 'water', 'meal', 'food', 'cal', 'calories']
STEPS_KEYWORDS   = ['steps', 'walked', 'walk', 'run', 'ran', 'jog', 'jogged', 'miles', 'km',
                    'distance', 'cardio', 'bike', 'biked', 'swim', 'swam']
SLEEP_KEYWORDS   = ['slept', 'sleep', 'hours sleep', 'hr sleep', 'hrs sleep', 'nap', 'woke', 'bed']
WEIGHT_KEYWORDS  = ['weigh', 'weight', 'lbs', 'kg', 'pounds', 'scale']
WORKOUT_KEYWORDS = ['workout', 'gym', 'lift', 'lifted', 'press', 'squat', 'deadlift',
                    'bench', 'sets', 'reps', 'exercise', 'training', 'trained', 'hiit', 'yoga', 'pilates']
WATER_KEYWORDS   = ['water', 'oz water', 'ml water', 'glasses water', 'hydrat']
MOOD_KEYWORDS    = ['mood', 'feel', 'feeling', 'energy', 'stress', 'tired', 'great', 'good', 'bad', 'anxious']

def classify_log(body):
    """Classify the SMS log entry into a fitness category."""
    lower = body.lower()
    if any(k in lower for k in WORKOUT_KEYWORDS): return 'workout'
    if any(k in lower for k in SLEEP_KEYWORDS):   return 'sleep'
    if any(k in lower for k in STEPS_KEYWORDS):   return 'movement'
    if any(k in lower for k in WEIGHT_KEYWORDS):  return 'weight'
    if any(k in lower for k in WATER_KEYWORDS):   return 'water'
    if any(k in lower for k in FOOD_KEYWORDS):    return 'food'
    if any(k in lower for k in MOOD_KEYWORDS):    return 'mood'
    return 'general'

def extract_numbers(text):
    """Extract first number found in text."""
    nums = re.findall(r'\d+\.?\d*', text)
    return float(nums[0]) if nums else None

def get_user_mode(phone):
    """Return current routing mode for the user: 'sos' or 'fitness' (default)."""
    try:
        prefs = db.collection('users').document(phone).collection('settings').document('prefs').get()
        if prefs.exists:
            return prefs.to_dict().get('mode', 'fitness')
    except Exception as e:
        app.logger.error(f"get_user_mode error: {e}")
    return 'fitness'

def ai_coach_reply(body, log_type, phone):
    """Get a brief AI coach reply for the logged entry."""
    try:
        # Get last 5 fitness logs for context
        recent = list(db.collection('users').document(phone)
                        .collection('fitness').order_by('timestamp', direction='DESCENDING')
                        .limit(5).stream())
        ctx = '\n'.join([f"- {r.to_dict().get('body','')}" for r in recent])

        msg = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=120,
            system=(
                "You are a supportive, concise SMS fitness coach. "
                "Reply in 1-2 short sentences max. Be encouraging, specific, and practical. "
                "Never exceed 160 characters if possible. No emojis unless the user used them."
            ),
            messages=[{"role": "user", "content": (
                f"User logged ({log_type}): {body}\n"
                f"Recent logs:\n{ctx}\n"
                "Give a brief coach response."
            )}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        app.logger.error(f"AI coach reply error: {e}")
        return None

# ── Stripe helpers ───────────────────────────────────────────────────────────
def find_phone_by_email(email):
    try:
        email_docs = db.collection_group('emails').where(
            filter=firestore.field_filter('email', '==', email)
        ).limit(5).stream()
        for doc in email_docs:
            path_parts = doc.reference.path.split('/')
            if len(path_parts) >= 2 and path_parts[0] == 'users':
                return path_parts[1]
    except Exception as e:
        app.logger.error(f"find_phone_by_email error: {e}")
    return None

def upgrade_user_plan(email, plan, subscription_id=None):
    phone = find_phone_by_email(email)
    if not phone:
        app.logger.error(f"upgrade_user_plan: no phone found for {email}")
        return
    profile_ref = db.collection('users').document(phone).collection('profile').document('data')
    update_data = {
        'plan': plan,
        'planUpdatedAt': firestore.SERVER_TIMESTAMP,
        'stripeEmail': email,
    }
    if subscription_id:
        update_data['stripeSubscriptionId'] = subscription_id
    profile_ref.set(update_data, merge=True)
    app.logger.info(f"Upgraded {phone} to {plan}")

# ── Stripe webhook ───────────────────────────────────────────────────────────
@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError as e:
        app.logger.error(f"Stripe sig error: {e}")
        return jsonify({"error": "Invalid signature"}), 400

    et  = event["type"]
    obj = event["data"]["object"]

    if et == "checkout.session.completed":
        email = obj.get("customer_email") or obj.get("customer_details", {}).get("email")
        meta  = obj.get("metadata", {})
        plan  = meta.get("plan", "coach")
        sub_id = obj.get("subscription")
        if email:
            upgrade_user_plan(email, plan, sub_id)

    elif et in ("customer.subscription.updated", "customer.subscription.deleted"):
        status = obj.get("status")
        sub_id = obj.get("id")
        plan = "track" if et == "customer.subscription.deleted" else obj.get("metadata", {}).get("plan", "coach")
        try:
            customer = stripe.Customer.retrieve(obj.get("customer"))
            email = customer.get("email")
            if email:
                upgrade_user_plan(email, plan, sub_id)
        except Exception as e:
            app.logger.error(f"Stripe customer retrieve error: {e}")

    return jsonify({"status": "ok"}), 200

# ── Helpers ──────────────────────────────────────────────────────────────────
def enc(email):
    return email.replace('.', '_dot_').replace('@', '_at_')

def find_email_key(phone):
    try:
        docs = list(db.collection('users').document(phone)
                      .collection('emails').stream())
        if docs:
            return docs[0].id
    except Exception as e:
        app.logger.error(f"find_email_key error: {e}")
    return enc(phone)

def valid_twilio(req):
    if not VALIDATE_TWILIO:
        return True
    v = RequestValidator(TWILIO_AUTH_TOKEN)
    url = req.url
    return v.validate(url, req.form, req.headers.get('X-Twilio-Signature', ''))

# ── Inbound SMS ──────────────────────────────────────────────────────────────
@app.route("/sms/inbound", methods=["POST"])
def inbound_sms():
    if not valid_twilio(request):
        return jsonify({"error": "Forbidden"}), 403

    phone   = request.form.get("From", "").strip()
    body    = request.form.get("Body", "").strip()
    sid     = request.form.get("MessageSid")

    app.logger.info(f"Inbound SMS — From: '{phone}' Body: '{body[:60]}' SID: {sid}")

    resp = MessagingResponse()

    if not phone:
        resp.message("Unable to process — sender phone number not detected.")
        return str(resp), 200, {"Content-Type": "text/xml"}

    upper = body.upper().strip()

    # ── STOP ──
    if upper == "STOP":
        resp.message("You have been unsubscribed from FitLogs. Reply START to re-subscribe.")
        return str(resp), 200, {"Content-Type": "text/xml"}

    # ── START ──
    if upper == "START":
        resp.message("Welcome back to FitLogs! 💪 Text your meals, steps, sleep, or workouts anytime. Reply HELP for commands.")
        return str(resp), 200, {"Content-Type": "text/xml"}

    # ── HELP ──
    if upper == "HELP":
        resp.message(
            "FitLogs commands:\n"
            "• Log food: 'Had oatmeal and coffee'\n"
            "• Log steps: 'Walked 8000 steps'\n"
            "• Log sleep: 'Slept 7 hrs'\n"
            "• Log workout: 'Gym 45min, chest day'\n"
            "• SUMMARY — today's summary\n"
            "• COACH [question] — ask your AI coach\n"
            "Reply STOP to unsubscribe."
        )
        return str(resp), 200, {"Content-Type": "text/xml"}

    # ── SUMMARY ──
    if upper == "SUMMARY":
        try:
            today_str = datetime.now(timezone.utc).date().isoformat()
            logs = [
                r.to_dict() for r in
                db.collection('users').document(phone).collection('fitness').stream()
                if (r.to_dict().get('timestampLocal') or '')[:10] == today_str
            ]
            if not logs:
                resp.message("No logs yet today. Text your meals, steps, or workouts to get started!")
            else:
                lines = [f"Today's FitLogs ({today_str}):"]
                for log in logs[-5:]:
                    t = (log.get('timestampLocal') or '')[11:16]
                    b = (log.get('body') or '')[:60]
                    lines.append(f"• [{t}] {b}")
                resp.message('\n'.join(lines))
        except Exception as e:
            app.logger.error(f"Summary error: {e}")
            resp.message("Could not fetch summary. Try again later.")
        return str(resp), 200, {"Content-Type": "text/xml"}

    # ── COACH question ──
    if upper.startswith("COACH ") or upper.startswith("COACH:"):
        question = body[6:].strip()
        if not question:
            resp.message("What would you like to ask your coach? Try: COACH How much protein should I eat?")
            return str(resp), 200, {"Content-Type": "text/xml"}
        try:
            msg = claude_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=200,
                system=(
                    "You are a knowledgeable, supportive fitness coach replying via SMS. "
                    "Keep replies under 200 characters. Be direct and practical."
                ),
                messages=[{"role": "user", "content": question}]
            )
            reply = msg.content[0].text.strip()
            resp.message(reply)
        except Exception as e:
            app.logger.error(f"Coach reply error: {e}")
            resp.message("Your coach is unavailable right now. Try again in a moment.")
        return str(resp), 200, {"Content-Type": "text/xml"}

    # ── Standard log — route to fitness/ or sos/ based on user mode ──
    log_type = classify_log(body)
    value    = extract_numbers(body)
    now_iso  = datetime.now(timezone.utc).isoformat()
    mode     = get_user_mode(phone)

    base_record = {
        "body":           body,
        "messageSid":     sid,
        "from":           phone,
        "timestamp":      firestore.SERVER_TIMESTAMP,
        "timestampLocal": now_iso,
        "source":         "sms",
        "type":           log_type,
        "value":          value,
    }

    try:
        user_ref = db.collection('users').document(phone)

        # 1. Always write to texts/ — immutable audit trail of every inbound SMS
        texts_record = {**base_record, "mode": mode}
        user_ref.collection('texts').document(sid).set(texts_record)
        app.logger.info(f"Saved to texts: users/{phone}/texts/{sid}")

        # 2. Route to fitness/ or sos/ based on mode
        if mode == 'sos':
            user_ref.collection('sos').document(sid).set(base_record)
            app.logger.info(f"Routed to sos: users/{phone}/sos/{sid}")
        else:
            user_ref.collection('fitness').document(sid).set(base_record)
            # Also keep legacy emails subcollection path for existing dashboard reads
            email_key = find_email_key(phone)
            user_ref.collection('emails').document(email_key).collection('logs').document(sid).set(base_record)
            app.logger.info(f"Routed to fitness: users/{phone}/fitness/{sid} type={log_type}")

    except Exception as e:
        app.logger.error(f"Firestore write failed: {e}")

    # AI coach reply (only for fitness mode — no coaching on SOS logs)
    if mode == 'sos':
        resp.message("🔒 Logged to your private journal.")
    else:
        coach_reply = ai_coach_reply(body, log_type, phone)
        if coach_reply:
            resp.message(coach_reply)
        else:
            resp.message(f"✅ Logged to FitLogs ({log_type}). View your dashboard at fitlogs.ai")

    return str(resp), 200, {"Content-Type": "text/xml"}

# ── Twilio Verify OTP ────────────────────────────────────────────────────────
@app.route("/verify/send", methods=["POST", "OPTIONS"])
def verify_send():
    if request.method == "OPTIONS":
        return '', 204
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    phone = data.get("phone", "").strip()
    if not phone:
        return jsonify({"error": "Phone number required"}), 400

    if not phone.startswith("+1") or len(phone.replace("+1","").replace(" ","").replace("-","")) != 10:
        return jsonify({"error": "Only US phone numbers (+1) are supported"}), 400

    if not VERIFY_SERVICE_SID:
        return jsonify({"error": "Verify service not configured"}), 500

    try:
        verification = twilio_client.verify.v2.services(VERIFY_SERVICE_SID) \
            .verifications.create(to=phone, channel="sms")
        app.logger.info(f"OTP sent to {phone} - status: {verification.status}")
        return jsonify({"status": "ok", "message": "Verification code sent"}), 200
    except Exception as e:
        app.logger.error(f"Twilio Verify send error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/verify/check", methods=["POST", "OPTIONS"])
def verify_check():
    if request.method == "OPTIONS":
        return '', 204
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    phone = data.get("phone", "").strip()
    code  = data.get("code", "").strip()

    if not phone or not code:
        return jsonify({"error": "Phone and code required"}), 400

    if not VERIFY_SERVICE_SID:
        return jsonify({"error": "Verify service not configured"}), 500

    try:
        check = twilio_client.verify.v2.services(VERIFY_SERVICE_SID) \
            .verification_checks.create(to=phone, code=code)
        app.logger.info(f"OTP check for {phone} - status: {check.status}")
        if check.status == "approved":
            return jsonify({"status": "approved"}), 200
        else:
            return jsonify({"status": "rejected", "error": "Invalid or expired code"}), 400
    except Exception as e:
        app.logger.error(f"Twilio Verify check error: {e}")
        return jsonify({"error": str(e)}), 500

# ── Claude Insights / AI Coach Chat ─────────────────────────────────────────
@app.route("/insights", methods=["POST", "OPTIONS"])
def insights():
    """Chat with Claude AI coach about user's fitness logs."""
    if request.method == "OPTIONS":
        return app.make_default_options_response()
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    phone       = data.get("phone", "").strip()
    records_ctx = data.get("records", "")
    messages    = data.get("messages", [])

    if not phone or not messages:
        return jsonify({"error": "Missing phone or messages"}), 400

    system_prompt = f"""You are an expert AI fitness coach for FitLogs, a personal health and fitness tracking platform.
You have access to the user's fitness logs and help them understand their progress, identify patterns, and improve their health.

The user's recent log data:
{records_ctx}

Guidelines:
- Be encouraging, specific, and actionable
- Reference specific dates, meals, workouts, or metrics from the logs when relevant
- Assess fitness trends: consistency, calories, sleep quality, activity levels, hydration
- Provide practical recommendations for nutrition, recovery, and training
- Never make medical diagnoses — suggest consulting a doctor for health concerns
- Keep responses clear and motivating"""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=system_prompt,
            messages=messages
        )
        reply = message.content[0].text
        app.logger.info(f"Insights response for {phone}: {len(reply)} chars")
        return jsonify({"reply": reply}), 200
    except Exception as e:
        app.logger.error(f"Insights error: {e}")
        return jsonify({"error": str(e)}), 500

# ── Account — Update Phone ───────────────────────────────────────────────────
@app.route("/account/update-phone", methods=["POST", "OPTIONS"])
def update_phone():
    if request.method == "OPTIONS":
        return '', 204
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400

    old_phone = data.get("old_phone", "").strip()
    new_phone = data.get("new_phone", "").strip()

    if not old_phone or not new_phone:
        return jsonify({"error": "Missing phone numbers"}), 400
    if old_phone == new_phone:
        return jsonify({"error": "New number is same as current"}), 400

    app.logger.info(f"Phone migration: {old_phone} → {new_phone}")

    try:
        # 1. Copy profile
        old_profile_ref = db.collection('users').document(old_phone).collection('profile').document('data')
        old_profile = old_profile_ref.get()
        if old_profile.exists:
            profile_data = old_profile.to_dict()
            profile_data['phone'] = new_phone
            db.collection('users').document(new_phone).collection('profile').document('data').set(profile_data)

        # 2. Copy emails subcollection
        old_emails = db.collection('users').document(old_phone).collection('emails').stream()
        for email_doc in old_emails:
            db.collection('users').document(new_phone).collection('emails').document(email_doc.id).set(email_doc.to_dict())

        # 3. Copy all logs
        old_logs = db.collection('users').document(old_phone).collection('logs').stream()
        migrated = 0
        for log in old_logs:
            log_data = log.to_dict()
            log_data['from'] = new_phone
            db.collection('users').document(new_phone).collection('logs').document(log.id).set(log_data)
            migrated += 1

        # 4. Delete old data
        old_logs2 = db.collection('users').document(old_phone).collection('logs').stream()
        for log in old_logs2:
            log.reference.delete()

        old_emails2 = db.collection('users').document(old_phone).collection('emails').stream()
        for email_doc in old_emails2:
            email_doc.reference.delete()

        old_profile_ref.delete()
        db.collection('users').document(old_phone).delete()

        app.logger.info(f"Migration complete: {migrated} logs moved")
        return jsonify({"status": "ok", "migrated": migrated}), 200

    except Exception as e:
        app.logger.error(f"Migration error: {e}")
        return jsonify({"error": str(e)}), 500

# ── Email helpers ────────────────────────────────────────────────────────────
def send_email(to_email, subject, html_body):
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = f"FitLogs <{ZOHO_EMAIL}>"
        msg['To']      = to_email
        msg.attach(MIMEText(html_body, 'html'))
        with smtplib.SMTP_SSL(SMTP_HOST, 465) as server:
            server.login(ZOHO_EMAIL, ZOHO_PASSWORD)
            server.sendmail(ZOHO_EMAIL, to_email, msg.as_string())
        app.logger.info(f"Email sent to {to_email}: {subject}")
        return True
    except Exception as e:
        app.logger.error(f"Email send error to {to_email}: {e}")
        return False

def get_user_email(phone):
    try:
        docs = db.collection('users').document(phone).collection('emails').stream()
        first = None
        for doc in docs:
            data = doc.to_dict()
            if data.get('primary'):
                return data.get('email', '')
            if not first:
                first = data.get('email', '')
        return first or ''
    except Exception as e:
        app.logger.error(f"get_user_email error for {phone}: {e}")
        return ''

def build_daily_email(phone, logs, date_str):
    count = len(logs)
    rows = ''
    for r in logs:
        ts   = (r.get('timestampLocal') or '')[:16].replace('T', ' ') or 'Unknown'
        body = (r.get('body') or '')[:120]
        ltype = (r.get('type') or 'general').title()
        rows += (
            '<tr><td style="padding:10px 0;border-bottom:1px solid #e2e8f0;font-size:13px;color:#0f172a;vertical-align:top;">'
            f'<div style="color:#64748b;font-size:11px;margin-bottom:3px;">{ts} &middot; <span style="color:#22c55e">{ltype}</span></div>'
            f'<div>{body}</div></td></tr>'
        )
    return (
        '<!DOCTYPE html><html><head><meta charset="UTF-8"></head>'
        '<body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,Helvetica Neue,Arial,sans-serif;">'
        '<div style="max-width:560px;margin:32px auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e2e8f0;">'
        '<div style="background:#15803d;padding:24px 28px;">'
        '<span style="font-size:20px;font-weight:800;color:#ffffff;letter-spacing:-0.5px;">Fit<span style="color:#dcfce7;">Logs</span></span>'
        '<span style="color:rgba(255,255,255,0.7);font-size:13px;margin-left:8px;">Daily Summary</span></div>'
        '<div style="padding:24px 28px;">'
        f'<div style="font-size:22px;font-weight:700;color:#0f172a;margin-bottom:4px;">{count} log{"s" if count != 1 else ""} today</div>'
        f'<div style="font-size:13px;color:#64748b;margin-bottom:20px;">{date_str}</div>'
        f'<table style="width:100%;border-collapse:collapse;">{rows}</table>'
        '<div style="margin-top:24px;text-align:center;">'
        '<a href="https://fitlogs.ai" style="display:inline-block;background:#22c55e;color:#000;font-weight:700;font-size:13px;padding:10px 24px;border-radius:8px;text-decoration:none;">View Dashboard</a>'
        '</div></div>'
        '<div style="padding:16px 28px;border-top:1px solid #e2e8f0;font-size:11px;color:#94a3b8;text-align:center;">'
        'FitLogs &middot; Athens Innovation and Development LLC &middot; Reply STOP to unsubscribe</div>'
        '</div></body></html>'
    )

def build_weekly_email(phone, logs, week_str):
    count = len(logs)
    try:
        logs_ctx = '\n'.join([
            f"[{(r.get('timestampLocal') or '')[:10]}] ({r.get('type','general')}) {(r.get('body') or '')[:150]}"
            for r in logs[:50]
        ])
        msg = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": (
                "Summarize this week's fitness logs in 3-4 sentences as a supportive coach. "
                "Note consistency, highlights, and one actionable tip for next week.\n\n"
                f"Logs:\n{logs_ctx}"
            )}]
        )
        summary_text = msg.content[0].text
    except Exception as e:
        app.logger.error(f"Weekly Claude error: {e}")
        summary_text = f"{count} fitness activities logged this week. Keep it up!"

    return (
        '<!DOCTYPE html><html><head><meta charset="UTF-8"></head>'
        '<body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,Helvetica Neue,Arial,sans-serif;">'
        '<div style="max-width:560px;margin:32px auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e2e8f0;">'
        '<div style="background:#15803d;padding:24px 28px;">'
        '<span style="font-size:20px;font-weight:800;color:#ffffff;">Fit<span style="color:#dcfce7;">Logs</span></span>'
        '<span style="color:rgba(255,255,255,0.7);font-size:13px;margin-left:8px;">Weekly Digest</span></div>'
        '<div style="padding:24px 28px;">'
        f'<div style="font-size:22px;font-weight:700;color:#0f172a;margin-bottom:4px;">{count} log{"s" if count != 1 else ""} this week</div>'
        f'<div style="font-size:13px;color:#64748b;margin-bottom:20px;">{week_str}</div>'
        f'<div style="background:#f0fdf4;border-left:3px solid #22c55e;padding:14px 16px;border-radius:0 8px 8px 0;font-size:13px;color:#0f172a;line-height:1.7;margin-bottom:16px;">{summary_text}</div>'
        '<div style="margin-top:24px;text-align:center;">'
        '<a href="https://fitlogs.ai" style="display:inline-block;background:#22c55e;color:#000;font-weight:700;font-size:13px;padding:10px 24px;border-radius:8px;text-decoration:none;">View Dashboard</a>'
        '</div></div>'
        '<div style="padding:16px 28px;border-top:1px solid #e2e8f0;font-size:11px;color:#94a3b8;text-align:center;">'
        'FitLogs &middot; Athens Innovation and Development LLC &middot; Reply STOP to unsubscribe</div>'
        '</div></body></html>'
    )

def send_daily_summaries():
    app.logger.info("FitLogs daily summary job started")
    today     = datetime.now(timezone.utc).date()
    today_str = today.isoformat()
    sent = 0
    try:
        for user_doc in db.collection('users').stream():
            phone = user_doc.id
            if not phone.startswith('+'):
                continue
            try:
                todays = [
                    r.to_dict() for r in
                    db.collection('users').document(phone).collection('fitness').stream()
                    if (r.to_dict().get('timestampLocal') or '')[:10] == today_str
                ]
                if not todays:
                    continue
                email = get_user_email(phone)
                if not email:
                    continue
                date_display = today.strftime('%A, %B %d, %Y')
                html = build_daily_email(phone, todays, date_display)
                if send_email(email, f"FitLogs Daily Summary - {date_display}", html):
                    sent += 1
            except Exception as e:
                app.logger.error(f"Daily summary error {phone}: {e}")
    except Exception as e:
        app.logger.error(f"Daily summary job error: {e}")
    app.logger.info(f"Daily summary complete - {sent} emails sent")

def send_weekly_digests():
    app.logger.info("FitLogs weekly digest job started")
    today      = datetime.now(timezone.utc).date()
    week_start = today - timedelta(days=7)
    sent = 0
    try:
        for user_doc in db.collection('users').stream():
            phone = user_doc.id
            if not phone.startswith('+'):
                continue
            try:
                weeks = [
                    r.to_dict() for r in
                    db.collection('users').document(phone).collection('fitness').stream()
                    if week_start.isoformat() <= (r.to_dict().get('timestampLocal') or '')[:10] <= today.isoformat()
                ]
                if not weeks:
                    continue
                email = get_user_email(phone)
                if not email:
                    continue
                week_str = f"{week_start.strftime('%b %d')} - {today.strftime('%b %d, %Y')}"
                html = build_weekly_email(phone, weeks, week_str)
                if send_email(email, f"FitLogs Weekly Digest - {week_str}", html):
                    sent += 1
            except Exception as e:
                app.logger.error(f"Weekly digest error {phone}: {e}")
    except Exception as e:
        app.logger.error(f"Weekly digest job error: {e}")
    app.logger.info(f"Weekly digest complete - {sent} emails sent")

# ── Cron job endpoints ───────────────────────────────────────────────────────
@app.route("/jobs/daily-summary", methods=["POST", "GET"])
def trigger_daily_summary():
    key = request.args.get('key', '')
    if key != os.environ.get('CRON_SECRET', 'fitlogs-cron-2026'):
        return jsonify({"error": "Unauthorized"}), 403
    threading.Thread(target=send_daily_summaries, daemon=True).start()
    return jsonify({"status": "started"}), 200

@app.route("/jobs/weekly-digest", methods=["POST", "GET"])
def trigger_weekly_digest():
    key = request.args.get('key', '')
    if key != os.environ.get('CRON_SECRET', 'fitlogs-cron-2026'):
        return jsonify({"error": "Unauthorized"}), 403
    threading.Thread(target=send_weekly_digests, daemon=True).start()
    return jsonify({"status": "started"}), 200

@app.route("/jobs/test-email", methods=["POST", "GET"])
def test_email():
    key = request.args.get('key', '')
    if key != os.environ.get('CRON_SECRET', 'fitlogs-cron-2026'):
        return jsonify({"error": "Unauthorized"}), 403
    to = request.args.get('to', ZOHO_EMAIL)
    ok = send_email(to, "FitLogs Email Test", "<h2>Email is working!</h2><p>Zoho SMTP configured correctly.</p>")
    return jsonify({"status": "ok" if ok else "failed"}), 200

# ── Health check ─────────────────────────────────────────────────────────────

# ── Photo Safety Check ──────────────────────────────────────────────────────
def screen_image_for_safety(base64_img):
    """
    First-pass safety screen using Claude.
    Returns (is_safe, reason) tuple.
    """
    try:
        screen_msg = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=100,
            system=(
                "You are a content safety screener. "
                "Your ONLY job is to determine if an image is safe for a family fitness app. "
                "Respond ONLY with valid JSON: "
                '{"safe": true/false, "reason": "brief reason if unsafe, empty string if safe"} '
                "Mark as UNSAFE if the image contains: explicit sexual content, nudity beyond typical fitness/medical context, "
                "graphic violence, illegal content, or anything inappropriate for a family health app. "
                "A person in workout clothes, a plate of food, or a body in fitness context is SAFE."
            ),
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64_img,
                        },
                    },
                    {"type": "text", "text": "Is this image safe for a family fitness app?"}
                ],
            }]
        )
        import json as json_lib
        raw = screen_msg.content[0].text.strip().replace("```json","").replace("```","").strip()
        result = json_lib.loads(raw)
        return result.get("safe", False), result.get("reason", "Content not appropriate")
    except Exception as e:
        app.logger.error(f"Safety screen error: {e}")
        # Fail safe — reject if screening fails
        return False, "Could not verify image safety"

# ── Photo Analysis ───────────────────────────────────────────────────────────
@app.route("/analyze-photo", methods=["POST", "OPTIONS"])
def analyze_photo():
    if request.method == "OPTIONS":
        return '', 204
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    photo_type  = data.get("type", "food")   # "food" or "body"
    base64_img  = data.get("image", "")
    goals       = data.get("goals", {})

    if not base64_img:
        return jsonify({"error": "No image provided"}), 400

    try:
        # ── SAFETY SCREEN FIRST — always runs before any analysis ──
        is_safe, reason = screen_image_for_safety(base64_img)
        if not is_safe:
            app.logger.warning(f"Image rejected by safety screen: {reason}")
            return jsonify({
                "error": "inappropriate_content",
                "message": "This image cannot be processed. Please upload a food photo or fitness-appropriate body photo."
            }), 400

        if photo_type == "food":
            system_prompt = (
                "You are a nutrition expert analyzing a food photo. "
                "Identify all visible food items and estimate calories. "
                "Be specific but concise. Never describe anything explicit or inappropriate. "
                "If the image does not contain food, say so and return empty results. "
                "Respond ONLY with valid JSON in this exact format: "
                '{"items": [{"name": "food name", "quantity": "amount", "calories": 123}], '
                '"total_calories": 456, "notes": "brief nutritional observation"}'
            )
        else:
            target_weight = goals.get("target_weight", "not set")
            target_waist  = goals.get("target_waist", "not set")
            fitness_goal  = goals.get("fitness_goal", "general fitness")
            system_prompt = (
                "You are a professional fitness coach analyzing a body photo. "
                "Provide constructive, encouraging observations about fitness level and body composition. "
                "NEVER make negative or demeaning comments. "
                "NEVER describe anything sexual or inappropriate — if the image is explicit, return an error. "
                f"The user's goals: target weight {target_weight}, target waist {target_waist}, goal: {fitness_goal}. "
                "Compare current appearance to typical progress toward these goals. "
                "Respond ONLY with valid JSON: "
                '{"observation": "professional fitness observation", '
                '"progress_notes": "how this compares to stated goals", '
                '"recommendations": ["tip 1", "tip 2", "tip 3"], '
                '"appropriate": true}'
            )

        message = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64_img,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Analyze this image and respond with JSON as instructed."
                    }
                ],
            }]
        )

        import json as json_lib
        raw = message.content[0].text.strip()
        # Strip markdown code fences if present
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json_lib.loads(raw)

        # Safety check for body photos
        if photo_type == "body" and not result.get("appropriate", True):
            return jsonify({"error": "Image not appropriate for analysis"}), 400

        return jsonify({"status": "ok", "type": photo_type, "result": result}), 200

    except Exception as e:
        app.logger.error(f"analyze_photo error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "fitlogs-webhook"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
