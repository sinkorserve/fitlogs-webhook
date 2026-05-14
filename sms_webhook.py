"""
fitlogs.ai -- SMS Webhook Server (Text-Only v1)
Twilio number: +18885965467

TEXT ONLY — MMS/images are explicitly rejected at the entry point.
No image handling code exists in this file. No base64, no binary,
no vision API calls. Zero surface area for media abuse.

Message types handled:
  - Food description (text) -> Claude text analysis -> calorie estimate -> Firestore
  - Body check-in (text)    -> Claude text analysis -> progress note   -> Firestore
  - Steps                   -> parse number         -> Firestore
  - Sleep                   -> parse hours          -> Firestore
  - Water                   -> parse amount         -> Firestore
  - Weight                  -> parse value          -> Firestore
  - Routines                -> parse keywords       -> Firestore
  - General note            -> store as-is          -> Firestore
  - CONFIRM / DISCARD       -> confirmation flow
  - CALORIES N              -> edit pending food entry
  - HELP                    -> usage instructions
  - STOP                    -> handled by Twilio automatically

Environment variables required:
    TWILIO_AUTH_TOKEN
    TWILIO_ACCOUNT_SID
    FIREBASE_CREDENTIALS_JSON
    ANTHROPIC_API_KEY
    VALIDATE_TWILIO   (set "false" in dev, "true" in prod)

Install: pip install flask twilio firebase-admin anthropic gunicorn
"""

from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
from datetime import datetime, timezone
import os, re, json, uuid
import firebase_admin
from firebase_admin import credentials, firestore
import anthropic

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
VALIDATE_TWILIO    = os.environ.get("VALIDATE_TWILIO", "true").lower() == "true"
FITLOGS_NUMBER     = "+18885965467"

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Firebase ──────────────────────────────────────────────────────
if not firebase_admin._apps:
    creds_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    if creds_json:
        cred = credentials.Certificate(json.loads(creds_json))
    else:
        cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ── Claude prompts (text-only) ────────────────────────────────────

FOOD_TEXT_SYSTEM = """You are a nutrition analyst for FitLogs, an SMS health tracking app.
The user has described a meal or food item in text. Estimate the nutrition and respond
ONLY with a JSON object — no markdown, no preamble, no explanation.

Format:
{
  "meal_description": "clean summary of what was described",
  "estimated_calories": 450,
  "protein_g": 35,
  "carbs_g": 40,
  "fat_g": 12,
  "confidence": "high|medium|low",
  "notes": "brief note about assumptions made or items that affected the estimate"
}

Rules:
- Be conservative on calories when uncertain — users can edit up
- confidence is high if the meal is specific and common, medium if vague, low if very unclear
- Always provide your best estimate — never refuse
- If the user mentions a restaurant dish, use standard portion estimates for that dish
- Do not ask follow-up questions — make your best estimate from what was given"""

BODY_TEXT_SYSTEM = """You are a health progress coach for FitLogs, an SMS health tracking app.
The user has described their body or physical progress in text. Provide an encouraging,
constructive analysis and respond ONLY with a JSON object — no markdown, no preamble.

Format:
{
  "observation": "acknowledge what the user described, reframe constructively",
  "progress_note": "encouraging observation about their direction based on what they described",
  "recommendation": "one specific, actionable suggestion",
  "sentiment": "positive|neutral|needs_encouragement"
}

Rules:
- Never make medical claims or diagnoses
- Always be encouraging — focus on effort and direction, not results
- Do not comment on weight negatively
- If the user seems discouraged, lead with encouragement
- Keep the observation grounded in what they actually described"""

# ── Helpers ───────────────────────────────────────────────────────

def now_utc():
    return datetime.now(timezone.utc)

def get_or_create_user(phone: str) -> dict:
    users_ref = db.collection("users")
    query = users_ref.where(
        filter=firestore.FieldFilter("phone", "==", phone)
    ).limit(1).get()
    if query:
        doc = query[0]
        return {"id": doc.id, **doc.to_dict()}
    user_id = str(uuid.uuid4())
    user_data = {
        "phone": phone,
        "created_at": now_utc(),
        "tier": "free",
        "goals": {
            "daily_calories": 2000,
            "daily_steps": 10000,
            "goal_description": ""
        },
        "health_metrics": {
            "nutrition": 0,
            "activity": 0,
            "body_composition": 0,
            "sleep": 0,
            "hydration": 0,
            "strength": 0,
            "recovery": 0
        }
    }
    users_ref.document(user_id).set(user_data)
    return {"id": user_id, **user_data}

def save_log(user_id: str, log_type: str, data: dict) -> str:
    """
    Save a text-only log entry to Firestore.
    Strips any binary or image data at the storage layer as a hard guardrail.
    No image handling exists in this codebase but this ensures it can never
    slip through even if future code accidentally passes binary data.
    """
    forbidden_keys = [
        "image", "photo", "media", "binary", "bytes", "blob",
        "base64", "image_data", "image_bytes", "media_data",
        "image_b64", "file", "attachment", "video", "audio"
    ]
    clean_data = {
        k: v for k, v in data.items()
        if not any(f in k.lower() for f in forbidden_keys)
        and not isinstance(v, (bytes, bytearray))
    }
    log_id = str(uuid.uuid4())
    entry = {
        "id": log_id,
        "type": log_type,
        "timestamp": now_utc(),
        "source": "sms",
        **clean_data
    }
    db.collection("users").document(user_id).collection("logs").document(log_id).set(entry)
    return log_id

def get_pending(user_id: str) -> dict | None:
    ref = db.collection("users").document(user_id).collection("pending").document("confirmation")
    doc = ref.get()
    return doc.to_dict() if doc.exists else None

def set_pending(user_id: str, data: dict):
    ref = db.collection("users").document(user_id).collection("pending").document("confirmation")
    ref.set({**data, "created_at": now_utc()})

def clear_pending(user_id: str):
    ref = db.collection("users").document(user_id).collection("pending").document("confirmation")
    ref.delete()

# ── Claude text analysis ──────────────────────────────────────────

def analyze_food_text(description: str) -> dict:
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        system=FOOD_TEXT_SYSTEM,
        messages=[{"role": "user", "content": f"Estimate nutrition for: {description}"}]
    )
    raw = re.sub(r"```json|```", "", response.content[0].text.strip()).strip()
    return json.loads(raw)

def analyze_body_text(description: str, user_goal: str) -> dict:
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        system=BODY_TEXT_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"User goal: {user_goal or 'general health improvement'}\nUser says: {description}"
        }]
    )
    raw = re.sub(r"```json|```", "", response.content[0].text.strip()).strip()
    return json.loads(raw)

# ── Parsers ───────────────────────────────────────────────────────

STEP_PATTERN   = re.compile(r"(?:steps?|walked?|step count)[:\s]+(\d[\d,]*)", re.IGNORECASE)
SLEEP_PATTERN  = re.compile(r"(\d+(?:\.\d+)?)\s*(?:hr|hour|hrs|hours?)\s*(?:of\s*)?sleep|slept\s+(\d+(?:\.\d+)?)\s*(?:hr|hour|hrs|hours?)", re.IGNORECASE)
WATER_PATTERN  = re.compile(r"(\d+(?:\.\d+)?)\s*(?:l|liter|liters?|oz|ounces?)\s*(?:of\s*)?(?:water|h2o)", re.IGNORECASE)
WEIGHT_PATTERN = re.compile(r"(?:weight|weigh)[:\s]+(\d+(?:\.\d+)?)\s*(?:lbs?|kg|pounds?|kilos?)", re.IGNORECASE)

FOOD_PREFIXES = (
    "breakfast", "lunch", "dinner", "snack", "meal", "ate", "eating",
    "had", "food", "drink", "drank", "coffee", "smoothie", "shake",
    "burger", "pizza", "salad", "sandwich", "soup", "pasta", "rice",
    "chicken", "beef", "fish", "eggs", "oatmeal", "yogurt", "fruit"
)

BODY_PREFIXES = (
    "check-in", "checkin", "check in", "progress", "body", "feeling",
    "noticing", "looking", "physique", "shape", "muscles", "weight loss",
    "gaining", "losing", "building", "stronger", "leaner", "heavier"
)

ROUTINE_MAP = {
    "gym": "strength", "weights": "strength", "lifting": "strength",
    "strength": "strength", "resistance": "strength", "bench": "strength",
    "squat": "strength", "deadlift": "strength",
    "cardio": "cardio", "run": "cardio", "running": "cardio",
    "jog": "cardio", "jogging": "cardio", "bike": "cardio",
    "cycling": "cardio", "swim": "cardio", "swimming": "cardio",
    "hiit": "cardio", "treadmill": "cardio", "elliptical": "cardio",
    "yoga": "flexibility", "stretch": "flexibility", "stretching": "flexibility",
    "pilates": "flexibility", "mobility": "flexibility",
    "vitamins": "vitamins", "vitamin": "vitamins", "supplements": "vitamins",
    "protein shake": "vitamins", "creatine": "vitamins",
    "meditation": "mindfulness", "meditate": "mindfulness", "breathwork": "mindfulness",
    "rest day": "rest_day", "rest": "rest_day", "recovery": "rest_day",
    "off day": "rest_day",
}

def parse_steps(body: str) -> int | None:
    m = STEP_PATTERN.search(body)
    return int(m.group(1).replace(",", "")) if m else None

def parse_sleep(body: str) -> float | None:
    m = SLEEP_PATTERN.search(body)
    if m:
        val = m.group(1) or m.group(2)
        return float(val) if val else None
    return None

def parse_water(body: str) -> tuple[float, str] | None:
    m = WATER_PATTERN.search(body)
    if m:
        unit = "oz" if "oz" in m.group(2).lower() or "ounce" in m.group(2).lower() else "L"
        return float(m.group(1)), unit
    return None

def parse_weight(body: str) -> tuple[float, str] | None:
    m = WEIGHT_PATTERN.search(body)
    if m:
        unit = "lbs" if any(u in m.group(0).lower() for u in ["lb", "pound"]) else "kg"
        return float(m.group(1)), unit
    return None

def parse_routines(body: str) -> list[str]:
    found = []
    body_lower = body.lower()
    for keyword, routine_type in ROUTINE_MAP.items():
        if keyword in body_lower and routine_type not in found:
            found.append(routine_type)
    return found

def is_food_entry(body: str) -> bool:
    body_lower = body.lower()
    return any(p in body_lower for p in FOOD_PREFIXES)

def is_body_checkin(body: str) -> bool:
    body_lower = body.lower()
    return any(p in body_lower for p in BODY_PREFIXES)

# ── Response builders ─────────────────────────────────────────────

def food_confirm_msg(analysis: dict) -> str:
    conf = analysis.get("confidence", "medium")
    conf_tag = "" if conf == "high" else f" ({conf} confidence)"
    notes = analysis.get("notes", "")
    notes_line = f"\n📌 {notes}" if notes else ""
    return (
        f"🍽 Nutrition Estimate{conf_tag}\n\n"
        f"📋 {analysis.get('meal_description', 'Your meal')}\n"
        f"🔥 ~{analysis.get('estimated_calories', '?')} kcal\n"
        f"💪 Protein: {analysis.get('protein_g', '?')}g\n"
        f"🌾 Carbs: {analysis.get('carbs_g', '?')}g\n"
        f"🥑 Fat: {analysis.get('fat_g', '?')}g"
        f"{notes_line}\n\n"
        f"Reply CONFIRM to save, or CALORIES 520 to correct."
    )

def body_confirm_msg(analysis: dict) -> str:
    return (
        f"💪 Progress Note\n\n"
        f"🔍 {analysis.get('observation', '')}\n\n"
        f"📈 {analysis.get('progress_note', '')}\n\n"
        f"💡 {analysis.get('recommendation', '')}\n\n"
        f"Reply CONFIRM to save, or DISCARD to skip."
    )

def steps_msg(steps: int, goal: int = 10000) -> str:
    pct = min(int(steps / goal * 100), 100)
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    done = "🎯 Goal reached!" if steps >= goal else f"{goal - steps:,} to go"
    return f"🚶 {steps:,} steps logged\n{bar} {pct}%\n{done}"

WELCOME_MSG = (
    "👋 Welcome to FitLogs!\n\n"
    "Text anything to log it:\n\n"
    "🍽 FOOD — describe your meal\n"
    "   'lunch grilled chicken rice'\n\n"
    "🚶 STEPS — 'steps 8432'\n"
    "😴 SLEEP — 'slept 7 hours'\n"
    "💧 WATER — '2L water'\n"
    "🏋 WORKOUT — 'gym 45min'\n"
    "💊 HEALTH — 'vitamins taken'\n"
    "📊 CHECK-IN — 'progress: feeling stronger'\n\n"
    "Reply HELP anytime for this menu."
)

MMS_REJECT_MSG = (
    "📵 FitLogs is text-only in this version.\n\n"
    "To log food, describe it:\n"
    "'lunch grilled chicken rice salad'\n\n"
    "AI estimates calories from your description.\n"
    "Photo upload coming in a future version."
)

# ── Main webhook ──────────────────────────────────────────────────

@app.route("/sms", methods=["POST"])
def sms_webhook():
    if VALIDATE_TWILIO:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        sig = request.headers.get("X-Twilio-Signature", "")
        if not validator.validate(request.url, request.form, sig):
            return jsonify({"error": "Invalid signature"}), 403

    from_number = request.form.get("From", "")
    body        = request.form.get("Body", "").strip()
    num_media   = int(request.form.get("NumMedia", 0))
    resp        = MessagingResponse()

    if not from_number:
        return str(resp), 200

    # ── HARD BLOCK: reject all MMS ────────────────────────────────
    # Media URL is never accessed. No image data is fetched or touched.
    if num_media > 0:
        resp.message(MMS_REJECT_MSG)
        return str(resp), 200

    # Text-only from this point forward
    user      = get_or_create_user(from_number)
    user_id   = user["id"]
    user_goal = user.get("goals", {}).get("goal_description", "")
    body_upper = body.upper().strip()

    # HELP
    if body_upper in ("HELP", "?"):
        resp.message(WELCOME_MSG)
        return str(resp), 200

    # CONFIRM
    if body_upper == "CONFIRM":
        pending = get_pending(user_id)
        if pending:
            save_log(user_id, pending["type"], pending["data"])
            clear_pending(user_id)
            resp.message("✅ Saved to your FitLogs dashboard.")
        else:
            resp.message("Nothing pending. Log something first.")
        return str(resp), 200

    # DISCARD
    if body_upper == "DISCARD":
        clear_pending(user_id)
        resp.message("Entry discarded. Nothing saved.")
        return str(resp), 200

    # CALORIE CORRECTION
    cal_match = re.match(r"CALORIES?\s+(\d+)", body_upper)
    if cal_match:
        pending = get_pending(user_id)
        if pending and pending.get("type") == "food":
            pending["data"]["estimated_calories"] = int(cal_match.group(1))
            pending["data"]["user_edited"] = True
            set_pending(user_id, pending)
            resp.message(f"Updated to {cal_match.group(1)} kcal. Reply CONFIRM to save.")
        else:
            resp.message("No pending food entry to update.")
        return str(resp), 200

    # STEPS
    steps = parse_steps(body)
    if steps is not None:
        goal = user.get("goals", {}).get("daily_steps", 10000)
        save_log(user_id, "steps", {"steps": steps, "goal": goal})
        resp.message(steps_msg(steps, goal))
        return str(resp), 200

    # MULTI-LOG: sleep + water + weight + routines
    logged = []

    sleep_hrs = parse_sleep(body)
    if sleep_hrs is not None:
        save_log(user_id, "sleep", {"hours": sleep_hrs})
        logged.append(f"😴 {sleep_hrs} hrs sleep")

    water = parse_water(body)
    if water:
        amount, unit = water
        save_log(user_id, "hydration", {"amount": amount, "unit": unit})
        logged.append(f"💧 {amount}{unit} water")

    weight = parse_weight(body)
    if weight:
        amount, unit = weight
        save_log(user_id, "weight", {"weight": amount, "unit": unit})
        logged.append(f"⚖️ {amount} {unit}")

    emoji_map = {
        "strength": "🏋", "cardio": "🫁", "flexibility": "🧘",
        "vitamins": "💊", "mindfulness": "🧠", "rest_day": "🛌"
    }
    for r in parse_routines(body):
        save_log(user_id, "routine", {"routine_type": r, "raw_text": body})
        logged.append(f"{emoji_map.get(r, '✅')} {r.replace('_', ' ').title()}")

    if logged:
        resp.message("✅ Logged:\n" + "\n".join(logged))
        return str(resp), 200

    # BODY CHECK-IN
    if is_body_checkin(body):
        try:
            analysis = analyze_body_text(body, user_goal)
            set_pending(user_id, {
                "type": "body_checkin",
                "data": {
                    "observation": analysis.get("observation", ""),
                    "progress_note": analysis.get("progress_note", ""),
                    "recommendation": analysis.get("recommendation", ""),
                    "sentiment": analysis.get("sentiment", "neutral"),
                    "user_description": body,
                    "user_goal": user_goal,
                    "ai_analyzed": True
                }
            })
            resp.message(body_confirm_msg(analysis))
        except Exception as e:
            resp.message(f"Could not analyze check-in. Try again.\n{str(e)[:60]}")
        return str(resp), 200

    # FOOD ENTRY — text description, 3+ words, or food keyword detected
    if is_food_entry(body) or len(body.split()) >= 3:
        try:
            analysis = analyze_food_text(body)
            set_pending(user_id, {
                "type": "food",
                "data": {
                    "meal_description": analysis.get("meal_description", ""),
                    "estimated_calories": analysis.get("estimated_calories", 0),
                    "protein_g": analysis.get("protein_g", 0),
                    "carbs_g": analysis.get("carbs_g", 0),
                    "fat_g": analysis.get("fat_g", 0),
                    "confidence": analysis.get("confidence", "medium"),
                    "notes": analysis.get("notes", ""),
                    "raw_text": body,
                    "ai_analyzed": True,
                    "user_edited": False
                }
            })
            resp.message(food_confirm_msg(analysis))
        except Exception as e:
            resp.message(f"Could not estimate nutrition. Try again.\n{str(e)[:60]}")
        return str(resp), 200

    # FALLBACK
    if body:
        save_log(user_id, "note", {"text": body})
        resp.message("📝 Note saved.\n\nReply HELP to see logging options.")
    else:
        resp.message(WELCOME_MSG)

    return str(resp), 200

# ── Dashboard endpoints (text-only) ──────────────────────────────

@app.route("/analyze/food", methods=["POST"])
def analyze_food_endpoint():
    try:
        data        = request.get_json()
        description = data.get("description", "").strip()
        if not description:
            return jsonify({"error": "No food description provided"}), 400
        analysis = analyze_food_text(description)
        return jsonify({"success": True, "analysis": analysis})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/analyze/body", methods=["POST"])
def analyze_body_endpoint():
    try:
        data        = request.get_json()
        description = data.get("description", "").strip()
        user_goal   = data.get("user_goal", "")
        if not description:
            return jsonify({"error": "No description provided"}), 400
        analysis = analyze_body_text(description, user_goal)
        return jsonify({"success": True, "analysis": analysis})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/log/confirm", methods=["POST"])
def confirm_log():
    try:
        data     = request.get_json()
        user_id  = data.get("user_id", "")
        log_type = data.get("type", "")
        log_data = data.get("data", {})
        if not user_id or not log_type:
            return jsonify({"error": "Missing user_id or type"}), 400
        log_id = save_log(user_id, log_type, {**log_data, "confirmed_via": "dashboard"})
        return jsonify({"success": True, "log_id": log_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "fitlogs-webhook",
        "version": "1.0-text-only",
        "number": FITLOGS_NUMBER,
        "mms_enabled": False
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
