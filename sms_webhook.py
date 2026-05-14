"""
fitlogs.ai -- SMS Webhook Server
Handles inbound SMS/MMS from Twilio number +18885965467

Message types handled:
  - Food photo (MMS)     -> Claude Vision -> calorie/macro estimate -> Firestore (text only, image discarded)
  - Body check-in (MMS)  -> Claude Vision -> progress observation  -> Firestore (text only, image discarded)
  - Steps text           -> parse number  -> Firestore
  - Routine keywords     -> parse habits  -> Firestore
  - General note         -> store as log  -> Firestore
  - CONFIRM / EDIT       -> confirmation flow for AI estimates

Environment variables required:
    TWILIO_AUTH_TOKEN
    TWILIO_ACCOUNT_SID
    FIREBASE_CREDENTIALS_JSON
    ANTHROPIC_API_KEY
    VALIDATE_TWILIO  (set to "false" in dev)

Install: pip install flask twilio firebase-admin anthropic gunicorn requests
"""

from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
from datetime import datetime, timezone
import os, re, json, uuid, base64
import requests as http
import firebase_admin
from firebase_admin import credentials, firestore
import anthropic

app = Flask(__name__)

# ── Config ──────────────────────────────────────────────────────
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
VALIDATE_TWILIO    = os.environ.get("VALIDATE_TWILIO", "true").lower() == "true"
FITLOGS_NUMBER     = "+18885965467"

claude  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
twilio  = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ── Firebase ─────────────────────────────────────────────────────
if not firebase_admin._apps:
    creds_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    if creds_json:
        cred = credentials.Certificate(json.loads(creds_json))
    else:
        cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ── Claude system prompts ─────────────────────────────────────────

FOOD_SYSTEM = """You are a nutrition analyst for FitLogs, an SMS-based health tracking app.
The user has sent a photo of their food. Analyze it and respond ONLY with a JSON object, no markdown, no preamble.

Format:
{
  "meal_description": "brief description of what you see",
  "estimated_calories": 450,
  "protein_g": 35,
  "carbs_g": 40,
  "fat_g": 12,
  "confidence": "high|medium|low",
  "notes": "any relevant observations about portion size or ingredients you are uncertain about"
}

Rules:
- Be conservative on calories when uncertain — users can edit up, overestimates discourage them
- confidence is high if food is clearly identifiable, medium if partially obscured, low if very unclear
- Do not refuse to estimate — always provide your best guess with appropriate confidence level
- Never store or reference the image itself — only the nutritional analysis"""

BODY_SYSTEM = """You are a health progress analyst for FitLogs, an SMS-based health tracking app.
The user has sent a photo for their body progress check-in. Analyze visible physical indicators and respond ONLY with a JSON object, no markdown, no preamble.

Format:
{
  "observation": "qualitative description of what you observe — posture, visible muscle definition, body composition indicators",
  "compared_to_baseline": "if this appears to be a progress photo, note any visible changes — be encouraging but honest",
  "recommendation": "one actionable suggestion based on what you see",
  "confidence": "high|medium|low"
}

Rules:
- Never estimate body fat percentage — too inaccurate from photos and potentially harmful
- Focus on positive observable indicators: posture, muscle definition, overall appearance
- Be encouraging and constructive — never critical or discouraging
- Do not make medical claims
- The image is analyzed then immediately discarded — never stored
- Keep observations professional and clinically neutral"""

# ── Helpers ───────────────────────────────────────────────────────

def now_utc():
    return datetime.now(timezone.utc)

def get_or_create_user(phone: str) -> dict:
    """Get user doc by phone number, create if not exists."""
    users_ref = db.collection("users")
    query = users_ref.where(filter=firestore.FieldFilter("phone", "==", phone)).limit(1).get()
    if query:
        doc = query[0]
        return {"id": doc.id, **doc.to_dict()}
    # Create new user
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
    """Save a log entry to Firestore. Returns the log entry ID."""
    log_id = str(uuid.uuid4())
    entry = {
        "id": log_id,
        "type": log_type,
        "timestamp": now_utc(),
        "source": "sms",
        **data
    }
    db.collection("users").document(user_id).collection("logs").document(log_id).set(entry)
    return log_id

def get_pending_confirmation(user_id: str) -> dict | None:
    """Check if user has a pending AI analysis awaiting confirmation."""
    ref = db.collection("users").document(user_id).collection("pending").document("confirmation")
    doc = ref.get()
    return doc.to_dict() if doc.exists else None

def set_pending_confirmation(user_id: str, data: dict):
    ref = db.collection("users").document(user_id).collection("pending").document("confirmation")
    ref.set({**data, "created_at": now_utc()})

def clear_pending_confirmation(user_id: str):
    ref = db.collection("users").document(user_id).collection("pending").document("confirmation")
    ref.delete()

def fetch_mms_image(media_url: str) -> tuple[bytes, str]:
    """
    Fetch MMS image from Twilio URL into memory.
    Image bytes are used for Claude API call then immediately dropped.
    Nothing is written to disk or stored.
    """
    response = http.get(
        media_url,
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        timeout=15
    )
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "image/jpeg")
    # Normalize content type
    if "jpeg" in content_type or "jpg" in content_type:
        media_type = "image/jpeg"
    elif "png" in content_type:
        media_type = "image/png"
    elif "gif" in content_type:
        media_type = "image/gif"
    elif "webp" in content_type:
        media_type = "image/webp"
    else:
        media_type = "image/jpeg"
    return response.content, media_type

def analyze_food_photo(image_bytes: bytes, media_type: str) -> dict:
    """
    Send food photo to Claude Vision. Image stays in memory only.
    Returns parsed nutrition data as dict.
    """
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
        system=FOOD_SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_b64
                    }
                },
                {
                    "type": "text",
                    "text": "Analyze this food photo and provide nutrition estimates."
                }
            ]
        }]
    )
    # Image bytes go out of scope here — not stored anywhere
    raw = response.content[0].text.strip()
    # Strip markdown fences if present
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)

def analyze_body_photo(image_bytes: bytes, media_type: str, user_goal: str) -> dict:
    """
    Send body check-in photo to Claude Vision. Image stays in memory only.
    Returns parsed progress observation as dict.
    """
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
        system=BODY_SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_b64
                    }
                },
                {
                    "type": "text",
                    "text": f"This is a body progress check-in photo. User goal: {user_goal or 'not specified'}. Provide your analysis."
                }
            ]
        }]
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)

# ── Message parsers ───────────────────────────────────────────────

STEP_PATTERN    = re.compile(r"(?:steps?|walked?|step count)[:\s]+(\d+)", re.IGNORECASE)
SLEEP_PATTERN   = re.compile(r"(\d+(?:\.\d+)?)\s*(?:hr|hour|hrs|hours?)\s*(?:of\s*)?sleep", re.IGNORECASE)
WATER_PATTERN   = re.compile(r"(\d+(?:\.\d+)?)\s*(?:l|liter|liters?|oz|ounces?)\s*(?:of\s*)?(?:water|h2o)", re.IGNORECASE)
WEIGHT_PATTERN  = re.compile(r"(?:weight|weigh)[:\s]+(\d+(?:\.\d+)?)\s*(?:lbs?|kg|pounds?|kilos?)", re.IGNORECASE)

ROUTINE_KEYWORDS = {
    "gym": "strength",
    "strength": "strength",
    "weights": "strength",
    "lifting": "strength",
    "cardio": "cardio",
    "run": "cardio",
    "running": "cardio",
    "bike": "cardio",
    "cycling": "cardio",
    "swim": "cardio",
    "swimming": "cardio",
    "yoga": "flexibility",
    "stretch": "flexibility",
    "pilates": "flexibility",
    "vitamins": "vitamins",
    "supplements": "vitamins",
    "meditation": "mindfulness",
    "meditate": "mindfulness",
    "rest": "rest_day",
    "rest day": "rest_day",
    "recovery": "rest_day",
}

def parse_steps(body: str) -> int | None:
    m = STEP_PATTERN.search(body)
    return int(m.group(1)) if m else None

def parse_sleep(body: str) -> float | None:
    m = SLEEP_PATTERN.search(body)
    return float(m.group(1)) if m else None

def parse_water(body: str) -> tuple[float, str] | None:
    m = WATER_PATTERN.search(body)
    if m:
        return float(m.group(1)), m.group(2).lower()
    return None

def parse_weight(body: str) -> tuple[float, str] | None:
    m = WEIGHT_PATTERN.search(body)
    if m:
        unit = "lbs" if any(u in m.group(2).lower() for u in ["lb", "pound"]) else "kg"
        return float(m.group(1)), unit
    return None

def parse_routines(body: str) -> list[str]:
    found = []
    body_lower = body.lower()
    for keyword, routine_type in ROUTINE_KEYWORDS.items():
        if keyword in body_lower and routine_type not in found:
            found.append(routine_type)
    return found

def is_photo_checkin(body: str) -> bool:
    """Detect if user intends a body check-in photo vs food photo."""
    checkin_words = ["check", "checkin", "check-in", "progress", "body", "selfie", "mirror", "physique"]
    body_lower = body.lower()
    return any(word in body_lower for word in checkin_words)

# ── Response builders ─────────────────────────────────────────────

def build_food_confirmation_msg(analysis: dict) -> str:
    conf = analysis.get("confidence", "medium")
    conf_note = "" if conf == "high" else f" (confidence: {conf})"
    return (
        f"🍽 FitLogs Food Analysis{conf_note}\n\n"
        f"📋 {analysis.get('meal_description', 'Your meal')}\n"
        f"🔥 Calories: ~{analysis.get('estimated_calories', '?')} kcal\n"
        f"💪 Protein: {analysis.get('protein_g', '?')}g\n"
        f"🌾 Carbs: {analysis.get('carbs_g', '?')}g\n"
        f"🥑 Fat: {analysis.get('fat_g', '?')}g\n"
        f"\n{analysis.get('notes', '')}\n"
        f"\nReply CONFIRM to save, or reply with corrections e.g. CALORIES 520"
    )

def build_body_confirmation_msg(analysis: dict) -> str:
    return (
        f"📸 FitLogs Body Check-In\n\n"
        f"🔍 {analysis.get('observation', '')}\n\n"
        f"📈 {analysis.get('compared_to_baseline', '')}\n\n"
        f"💡 {analysis.get('recommendation', '')}\n"
        f"\nPhoto has been discarded. Reply CONFIRM to save this observation, or DISCARD to skip."
    )

def build_steps_msg(steps: int, goal: int = 10000) -> str:
    pct = min(int(steps / goal * 100), 100)
    bar_filled = pct // 10
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    return (
        f"🚶 {steps:,} steps logged\n"
        f"{bar} {pct}% of {goal:,} goal\n"
        f"{'🎯 Goal reached!' if steps >= goal else f'{goal - steps:,} to go'}"
    )

# ── Main webhook ──────────────────────────────────────────────────

@app.route("/sms", methods=["POST"])
def sms_webhook():
    # Twilio signature validation
    if VALIDATE_TWILIO:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        signature = request.headers.get("X-Twilio-Signature", "")
        url = request.url
        if not validator.validate(url, request.form, signature):
            return jsonify({"error": "Invalid Twilio signature"}), 403

    from_number = request.form.get("From", "")
    body        = request.form.get("Body", "").strip()
    num_media   = int(request.form.get("NumMedia", 0))
    resp        = MessagingResponse()

    if not from_number:
        return str(resp), 200

    # Get or create user
    user = get_or_create_user(from_number)
    user_id = user["id"]
    user_goal = user.get("goals", {}).get("goal_description", "")

    # ── CONFIRMATION FLOW ──────────────────────────────────────────
    body_upper = body.upper().strip()

    if body_upper == "CONFIRM":
        pending = get_pending_confirmation(user_id)
        if pending:
            save_log(user_id, pending["type"], pending["data"])
            clear_pending_confirmation(user_id)
            resp.message("✅ Saved to your FitLogs dashboard.")
        else:
            resp.message("No pending entry to confirm. Send a food or body photo to get started.")
        return str(resp), 200

    if body_upper == "DISCARD":
        clear_pending_confirmation(user_id)
        resp.message("Entry discarded. Nothing was saved.")
        return str(resp), 200

    # Calorie correction: CALORIES 520
    cal_match = re.match(r"CALORIES?\s+(\d+)", body_upper)
    if cal_match:
        pending = get_pending_confirmation(user_id)
        if pending and pending["type"] == "food":
            pending["data"]["estimated_calories"] = int(cal_match.group(1))
            pending["data"]["user_edited"] = True
            set_pending_confirmation(user_id, pending)
            resp.message(f"Updated to {cal_match.group(1)} kcal. Reply CONFIRM to save.")
        else:
            resp.message("No pending food entry to update.")
        return str(resp), 200

    # ── MMS PHOTO HANDLING ─────────────────────────────────────────
    if num_media > 0:
        media_url       = request.form.get("MediaUrl0", "")
        media_type_raw  = request.form.get("MediaContentType0", "image/jpeg")

        # Determine food vs body check-in
        intent_is_body = is_photo_checkin(body)

        try:
            # Fetch image into memory only — never written to disk
            image_bytes, media_type = fetch_mms_image(media_url)

            if intent_is_body:
                # Body progress check-in
                analysis = analyze_body_photo(image_bytes, media_type, user_goal)
                # Image bytes go out of scope here
                del image_bytes

                set_pending_confirmation(user_id, {
                    "type": "body_checkin",
                    "data": {
                        "observation": analysis.get("observation", ""),
                        "compared_to_baseline": analysis.get("compared_to_baseline", ""),
                        "recommendation": analysis.get("recommendation", ""),
                        "confidence": analysis.get("confidence", "medium"),
                        "user_goal": user_goal,
                        "ai_analyzed": True,
                        "image_stored": False
                    }
                })
                reply = build_body_confirmation_msg(analysis)

            else:
                # Food photo
                analysis = analyze_food_photo(image_bytes, media_type)
                del image_bytes

                set_pending_confirmation(user_id, {
                    "type": "food",
                    "data": {
                        "meal_description": analysis.get("meal_description", ""),
                        "estimated_calories": analysis.get("estimated_calories", 0),
                        "protein_g": analysis.get("protein_g", 0),
                        "carbs_g": analysis.get("carbs_g", 0),
                        "fat_g": analysis.get("fat_g", 0),
                        "confidence": analysis.get("confidence", "medium"),
                        "notes": analysis.get("notes", ""),
                        "ai_analyzed": True,
                        "image_stored": False,
                        "user_edited": False
                    }
                })
                reply = build_food_confirmation_msg(analysis)

        except Exception as e:
            reply = f"Sorry, could not analyze your photo. Please try again or type your entry manually.\nError: {str(e)[:80]}"

        resp.message(reply)
        return str(resp), 200

    # ── TEXT MESSAGE PARSING ───────────────────────────────────────
    logged = []

    # Steps
    steps = parse_steps(body)
    if steps is not None:
        goal = user.get("goals", {}).get("daily_steps", 10000)
        save_log(user_id, "steps", {"steps": steps, "goal": goal})
        resp.message(build_steps_msg(steps, goal))
        return str(resp), 200

    # Sleep
    sleep_hrs = parse_sleep(body)
    if sleep_hrs is not None:
        save_log(user_id, "sleep", {"hours": sleep_hrs})
        logged.append(f"😴 {sleep_hrs} hrs sleep")

    # Water
    water = parse_water(body)
    if water:
        amount, unit = water
        save_log(user_id, "hydration", {"amount": amount, "unit": unit})
        logged.append(f"💧 {amount}{unit} water")

    # Weight
    weight = parse_weight(body)
    if weight:
        amount, unit = weight
        save_log(user_id, "weight", {"weight": amount, "unit": unit})
        logged.append(f"⚖️ {amount} {unit}")

    # Routines
    routines = parse_routines(body)
    for r in routines:
        save_log(user_id, "routine", {"routine_type": r, "raw_text": body})
        emoji_map = {
            "strength": "🏋",
            "cardio": "🫁",
            "flexibility": "🧘",
            "vitamins": "💊",
            "mindfulness": "🧠",
            "rest_day": "🛌"
        }
        logged.append(f"{emoji_map.get(r, '✅')} {r.replace('_', ' ').title()}")

    if logged:
        summary = "✅ Logged to FitLogs:\n" + "\n".join(logged)
        resp.message(summary)
        return str(resp), 200

    # Fallback — save as general note
    if body:
        save_log(user_id, "note", {"text": body})
        resp.message("📝 Note saved to your FitLogs log.\n\nTip: Send a food photo, or text 'steps 8000', 'slept 7 hours', '2L water', or 'gym 45min'")
    else:
        resp.message(
            "👋 Welcome to FitLogs!\n\n"
            "What you can text:\n"
            "📸 Food photo → calorie estimate\n"
            "📸 Selfie + 'check-in' → body progress\n"
            "🚶 'steps 8000'\n"
            "😴 '7 hours sleep'\n"
            "💧 '2L water'\n"
            "🏋 'gym 45min strength'\n"
            "💊 'vitamins taken'"
        )

    return str(resp), 200

# ── Dashboard analysis endpoint ────────────────────────────────────
# Called from the web dashboard when user uploads a photo manually
# Same analyze-and-discard policy — image bytes never leave this function

@app.route("/analyze/food", methods=["POST"])
def analyze_food_endpoint():
    """Accepts base64 image from dashboard, returns nutrition analysis as JSON."""
    try:
        data       = request.get_json()
        image_b64  = data.get("image_base64", "")
        media_type = data.get("media_type", "image/jpeg")
        user_id    = data.get("user_id", "")

        if not image_b64:
            return jsonify({"error": "No image provided"}), 400

        image_bytes = base64.b64decode(image_b64)
        analysis    = analyze_food_photo(image_bytes, media_type)
        del image_bytes  # Discard immediately

        return jsonify({"success": True, "analysis": analysis})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/analyze/body", methods=["POST"])
def analyze_body_endpoint():
    """Accepts base64 image from dashboard, returns body progress analysis as JSON."""
    try:
        data       = request.get_json()
        image_b64  = data.get("image_base64", "")
        media_type = data.get("media_type", "image/jpeg")
        user_id    = data.get("user_id", "")
        user_goal  = data.get("user_goal", "")

        if not image_b64:
            return jsonify({"error": "No image provided"}), 400

        image_bytes = base64.b64decode(image_b64)
        analysis    = analyze_body_photo(image_bytes, media_type, user_goal)
        del image_bytes

        return jsonify({"success": True, "analysis": analysis})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/log/confirm", methods=["POST"])
def confirm_log():
    """Save a confirmed (possibly user-edited) log entry from the dashboard."""
    try:
        data      = request.get_json()
        user_id   = data.get("user_id", "")
        log_type  = data.get("type", "")
        log_data  = data.get("data", {})

        if not user_id or not log_type:
            return jsonify({"error": "Missing user_id or type"}), 400

        log_id = save_log(user_id, log_type, {**log_data, "confirmed_via": "dashboard"})
        return jsonify({"success": True, "log_id": log_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "fitlogs-webhook", "number": FITLOGS_NUMBER})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
