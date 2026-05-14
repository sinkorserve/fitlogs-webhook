"""
fitlogs.ai -- SMS Webhook Server (Text-Only v1)
Twilio number: +18885965467

Tier routing:
  - Unregistered user -> onboarding message, link to fitlogs.ai
  - Track ($9.99)     -> logging only, AI parses and confirms
  - Coach ($19.99)    -> logging + real-time empathetic AI coaching
  - Trainer ($29.99)  -> same as Coach, trainer has read dashboard access

TEXT ONLY. No MMS. No image handling. Zero binary data.

Environment variables:
    TWILIO_AUTH_TOKEN
    TWILIO_ACCOUNT_SID
    FIREBASE_CREDENTIALS_JSON
    ANTHROPIC_API_KEY
    VALIDATE_TWILIO   ("false" in dev, "true" in prod)

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
FITLOGS_URL        = "https://fitlogs.ai"

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

# ── System prompts ────────────────────────────────────────────────

FOOD_SYSTEM = """You are a nutrition analyst for FitLogs, an SMS health tracking app for personal trainers and their clients.
The user has described a meal in text. Estimate nutrition and respond ONLY with JSON — no markdown, no preamble.

{
  "meal_description": "clean summary of what was described",
  "estimated_calories": 450,
  "protein_g": 35,
  "carbs_g": 40,
  "fat_g": 12,
  "confidence": "high|medium|low",
  "notes": "brief note about assumptions or uncertainties"
}

Rules:
- Be conservative on calories — users can edit up
- Always estimate, never refuse
- Use standard portions for named restaurant dishes
- confidence: high if specific, medium if vague, low if very unclear"""

COACH_SYSTEM = """You are an empathetic, knowledgeable AI health coach for FitLogs.
You support clients of personal trainers in staying consistent with their health goals.

Your personality:
- Warm, encouraging, and non-judgmental
- Direct and practical — no fluff
- You remember context from this conversation
- You celebrate small wins genuinely
- You ask one good follow-up question at a time
- You never diagnose or prescribe medically

Your role:
- Help users stay motivated and consistent
- Provide practical advice on nutrition, sleep, exercise, recovery
- Gently challenge excuses while validating feelings
- Connect today's behavior to their stated goals
- Keep responses concise — this is SMS, not an essay

If the user describes a food or asks about calories, give a brief estimate but stay in coaching mode.
If the user seems to be logging data (steps, sleep, water), acknowledge it warmly and ask how they are feeling.
Never say you are an AI unless directly asked. Respond as a coach would."""

BODY_SYSTEM = """You are a health progress coach for FitLogs.
The user has described their physical progress or how they are feeling in text.
Respond ONLY with JSON — no markdown, no preamble.

{
  "observation": "acknowledge what they described, reframe constructively",
  "progress_note": "encouraging observation about their direction",
  "recommendation": "one specific, actionable suggestion",
  "sentiment": "positive|neutral|needs_encouragement"
}

Rules:
- Always encouraging — focus on effort and direction
- Never comment on weight negatively
- No medical claims
- Grounded in what they actually described"""

# ── Helpers ───────────────────────────────────────────────────────

def now_utc():
    return datetime.now(timezone.utc)

def get_user(phone: str) -> dict | None:
    """Get user by phone. Returns None if not registered."""
    query = db.collection("users").where(
        filter=firestore.FieldFilter("phone", "==", phone)
    ).limit(1).get()
    if query:
        doc = query[0]
        return {"id": doc.id, **doc.to_dict()}
    return None

def create_user(phone: str) -> dict:
    """Create new user on first contact — unverified until they sign up at fitlogs.ai."""
    user_id = str(uuid.uuid4())
    user_data = {
        "phone": phone,
        "created_at": now_utc(),
        "tier": "unregistered",
        "onboarding_step": 1,
        "goals": {"daily_calories": 2000, "daily_steps": 10000, "goal_description": ""},
        "health_metrics": {
            "nutrition": 0, "training": 0, "cardio": 0,
            "sleep": 0, "hydration": 0, "adherence": 0, "recovery": 0
        }
    }
    db.collection("users").document(user_id).set(user_data)
    return {"id": user_id, **user_data}

def get_or_create_user(phone: str) -> dict:
    user = get_user(phone)
    return user if user else create_user(phone)

def save_log(user_id: str, log_type: str, data: dict) -> str:
    """Save text-only log entry. Strips any binary data at storage layer."""
    forbidden = ["image","photo","media","binary","bytes","blob","base64",
                 "image_data","image_bytes","file","attachment","video","audio"]
    clean = {k: v for k, v in data.items()
             if not any(f in k.lower() for f in forbidden)
             and not isinstance(v, (bytes, bytearray))}
    log_id = str(uuid.uuid4())
    db.collection("users").document(user_id).collection("logs").document(log_id).set({
        "id": log_id, "type": log_type, "timestamp": now_utc(), "source": "sms", **clean
    })
    return log_id

def get_pending(user_id: str) -> dict | None:
    ref = db.collection("users").document(user_id).collection("pending").document("confirmation")
    doc = ref.get()
    return doc.to_dict() if doc.exists else None

def set_pending(user_id: str, data: dict):
    db.collection("users").document(user_id).collection("pending").document("confirmation").set(
        {**data, "created_at": now_utc()}
    )

def clear_pending(user_id: str):
    db.collection("users").document(user_id).collection("pending").document("confirmation").delete()

def get_coach_history(user_id: str, limit: int = 10) -> list:
    """Get recent coach conversation history for context."""
    docs = db.collection("users").document(user_id).collection("coach_history")\
        .order_by("timestamp", direction=firestore.Query.DESCENDING).limit(limit).get()
    history = [{"role": d.get("role"), "content": d.get("content")} for d in reversed(docs)]
    return history

def save_coach_message(user_id: str, role: str, content: str):
    db.collection("users").document(user_id).collection("coach_history").add({
        "role": role, "content": content, "timestamp": now_utc()
    })

def get_recent_logs_summary(user_id: str) -> str:
    """Pull recent logs to give Claude coach context about the user."""
    try:
        docs = db.collection("users").document(user_id).collection("logs")\
            .order_by("timestamp", direction=firestore.Query.DESCENDING).limit(10).get()
        if not docs:
            return "No recent logs."
        summary = []
        for doc in docs:
            d = doc.to_dict()
            t = d.get("type", "")
            if t == "food":
                summary.append(f"Food: {d.get('meal_description','')} ~{d.get('estimated_calories',0)} kcal")
            elif t == "steps":
                summary.append(f"Steps: {d.get('steps',0):,}")
            elif t == "sleep":
                summary.append(f"Sleep: {d.get('hours',0)} hrs")
            elif t == "hydration":
                summary.append(f"Water: {d.get('amount',0)}{d.get('unit','L')}")
            elif t == "routine":
                summary.append(f"Routine: {d.get('routine_type','')}")
            elif t == "weight":
                summary.append(f"Weight: {d.get('weight',0)} {d.get('unit','lbs')}")
        return " | ".join(summary[:6]) if summary else "No recent logs."
    except Exception:
        return "No recent logs."

# ── Claude calls ──────────────────────────────────────────────────

def analyze_food_text(description: str) -> dict:
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        system=FOOD_SYSTEM,
        messages=[{"role": "user", "content": f"Estimate nutrition for: {description}"}]
    )
    raw = re.sub(r"```json|```", "", response.content[0].text.strip()).strip()
    return json.loads(raw)

def analyze_body_text(description: str, user_goal: str) -> dict:
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        system=BODY_SYSTEM,
        messages=[{"role": "user", "content": f"Goal: {user_goal or 'general health'}\nUser: {description}"}]
    )
    raw = re.sub(r"```json|```", "", response.content[0].text.strip()).strip()
    return json.loads(raw)

def coach_reply(user_id: str, user_message: str, user_goal: str) -> str:
    """Generate empathetic coach reply with conversation history and log context."""
    history = get_coach_history(user_id, limit=8)
    recent_logs = get_recent_logs_summary(user_id)

    system = COACH_SYSTEM + f"\n\nUser's recent activity: {recent_logs}"
    if user_goal:
        system += f"\nUser's stated goal: {user_goal}"

    messages = history + [{"role": "user", "content": user_message}]

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        system=system,
        messages=messages
    )
    reply = response.content[0].text.strip()

    # Save both sides to history
    save_coach_message(user_id, "user", user_message)
    save_coach_message(user_id, "assistant", reply)

    return reply

# ── Parsers ───────────────────────────────────────────────────────

STEP_PATTERN   = re.compile(r"(?:steps?|walked?|step count)[:\s]+(\d[\d,]*)", re.IGNORECASE)
SLEEP_PATTERN  = re.compile(r"(\d+(?:\.\d+)?)\s*(?:hr|hour|hrs|hours?)\s*(?:of\s*)?sleep|slept\s+(\d+(?:\.\d+)?)\s*(?:hr|hour|hrs|hours?)", re.IGNORECASE)
WATER_PATTERN  = re.compile(r"(\d+(?:\.\d+)?)\s*(?:l|liter|liters?|oz|ounces?)\s*(?:of\s*)?(?:water|h2o)", re.IGNORECASE)
WEIGHT_PATTERN = re.compile(r"(?:weight|weigh)[:\s]+(\d+(?:\.\d+)?)\s*(?:lbs?|kg|pounds?|kilos?)", re.IGNORECASE)

FOOD_WORDS = (
    "breakfast","lunch","dinner","snack","meal","ate","eating","had","food",
    "drink","drank","coffee","smoothie","shake","burger","pizza","salad",
    "sandwich","soup","pasta","rice","chicken","beef","fish","eggs","oatmeal",
    "yogurt","fruit","bar","protein","calories","kcal","toast","wrap","bowl"
)
BODY_WORDS = (
    "check-in","checkin","check in","progress","body","feeling","noticing",
    "looking","physique","shape","muscles","stronger","leaner","gaining",
    "losing","building","sore","tired","energized","motivated","sluggish"
)
ROUTINE_MAP = {
    "gym":"strength","weights":"strength","lifting":"strength","strength":"strength",
    "resistance":"strength","bench":"strength","squat":"strength","deadlift":"strength",
    "cardio":"cardio","run":"cardio","running":"cardio","jog":"cardio","jogging":"cardio",
    "bike":"cardio","cycling":"cardio","swim":"cardio","swimming":"cardio",
    "hiit":"cardio","treadmill":"cardio","elliptical":"cardio","walk":"cardio",
    "yoga":"flexibility","stretch":"flexibility","stretching":"flexibility",
    "pilates":"flexibility","mobility":"flexibility",
    "vitamins":"vitamins","vitamin":"vitamins","supplements":"vitamins","creatine":"vitamins",
    "meditation":"mindfulness","meditate":"mindfulness","breathwork":"mindfulness",
    "rest day":"rest_day","rest":"rest_day","recovery":"rest_day","off day":"rest_day",
}

def parse_steps(body):
    m = STEP_PATTERN.search(body)
    return int(m.group(1).replace(",","")) if m else None

def parse_sleep(body):
    m = SLEEP_PATTERN.search(body)
    if m:
        val = m.group(1) or m.group(2)
        return float(val) if val else None
    return None

def parse_water(body):
    m = WATER_PATTERN.search(body)
    if m:
        unit = "oz" if "oz" in m.group(2).lower() else "L"
        return float(m.group(1)), unit
    return None

def parse_weight(body):
    m = WEIGHT_PATTERN.search(body)
    if m:
        unit = "lbs" if any(u in m.group(0).lower() for u in ["lb","pound"]) else "kg"
        return float(m.group(1)), unit
    return None

def parse_routines(body):
    found = []
    bl = body.lower()
    for kw, rt in ROUTINE_MAP.items():
        if kw in bl and rt not in found:
            found.append(rt)
    return found

def is_food(body):
    bl = body.lower()
    return any(w in bl for w in FOOD_WORDS)

def is_body_checkin(body):
    bl = body.lower()
    return any(w in bl for w in BODY_WORDS)

def is_conversational(body):
    """Detect messages that are clearly conversational rather than logging."""
    triggers = ["feel","help","motivat","struggle","hard","difficult","why","how do","should i",
                "advice","tip","skipped","missed","can't","cannot","worried","scared","excited",
                "proud","happy","sad","frustrated","?"]
    bl = body.lower()
    return any(t in bl for t in triggers) or body.strip().endswith("?")

# ── Response builders ─────────────────────────────────────────────

def food_confirm_msg(a):
    conf = a.get("confidence","medium")
    tag = "" if conf=="high" else f" ({conf} confidence)"
    notes = a.get("notes","")
    nl = f"\n📌 {notes}" if notes else ""
    return (
        f"🍽 Nutrition Estimate{tag}\n\n"
        f"📋 {a.get('meal_description','Your meal')}\n"
        f"🔥 ~{a.get('estimated_calories','?')} kcal\n"
        f"💪 Protein: {a.get('protein_g','?')}g\n"
        f"🌾 Carbs: {a.get('carbs_g','?')}g\n"
        f"🥑 Fat: {a.get('fat_g','?')}g{nl}\n\n"
        f"Reply CONFIRM to save or CALORIES 520 to correct."
    )

def body_confirm_msg(a):
    return (
        f"💪 Progress Note\n\n"
        f"🔍 {a.get('observation','')}\n\n"
        f"📈 {a.get('progress_note','')}\n\n"
        f"💡 {a.get('recommendation','')}\n\n"
        f"Reply CONFIRM to save or DISCARD to skip."
    )

def steps_msg(steps, goal=10000):
    pct = min(int(steps/goal*100),100)
    bar = "█"*(pct//10) + "░"*(10-pct//10)
    done = "🎯 Goal reached!" if steps>=goal else f"{goal-steps:,} to go"
    return f"🚶 {steps:,} steps logged\n{bar} {pct}%\n{done}"

WELCOME_MSG = (
    "👋 Welcome to FitLogs!\n\n"
    "Sign up for your 30-day free trial at:\n"
    f"{FITLOGS_URL}\n\n"
    "Once registered, text anything to log it:\n"
    "🍽 'lunch grilled chicken rice'\n"
    "🚶 'steps 8432'\n"
    "😴 'slept 7 hours'\n"
    "🏋 'gym 45min strength'\n"
    "💬 Or just talk to your AI coach"
)

SECOND_MSG = (
    "🎉 Welcome to FitLogs Coach!\n\n"
    "Your 30-day free trial has started.\n\n"
    "You're on the Coach plan ($19.99/mo) which includes:\n"
    "✓ Real-time AI coaching conversations\n"
    "✓ Full dashboard at fitlogs.ai\n"
    "✓ AI calorie estimates from text\n"
    "✓ Weekly AI progress reports\n\n"
    "Text DOWNGRADE to switch to Track ($9.99/mo)\n"
    "Text STOP to cancel anytime\n"
    "No charge for 30 days.\n\n"
    "Now — what's your main health goal? Tell me and let's get started."
)

HELP_MSG = (
    "📋 FitLogs Help\n\n"
    "🍽 FOOD: describe your meal\n"
    "   'lunch chicken rice broccoli'\n\n"
    "🚶 STEPS: 'steps 8432'\n"
    "😴 SLEEP: 'slept 7 hours'\n"
    "💧 WATER: '2L water'\n"
    "🏋 WORKOUT: 'gym 45min strength'\n"
    "💊 HEALTH: 'vitamins taken'\n"
    "💬 COACH: just talk naturally\n\n"
    "CONFIRM — save pending entry\n"
    "DISCARD — discard pending entry\n"
    "CALORIES N — correct calorie estimate\n"
    "DOWNGRADE — switch to Track ($9.99)\n"
    "STOP — cancel subscription\n\n"
    f"Dashboard: {FITLOGS_URL}"
)

MMS_MSG = (
    "📵 FitLogs is text-only.\n\n"
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
        sig = request.headers.get("X-Twilio-Signature","")
        if not validator.validate(request.url, request.form, sig):
            return jsonify({"error":"Invalid signature"}), 403

    from_number = request.form.get("From","")
    body        = request.form.get("Body","").strip()
    num_media   = int(request.form.get("NumMedia",0))
    resp        = MessagingResponse()

    if not from_number:
        return str(resp), 200

    # ── HARD BLOCK: MMS ───────────────────────────────────────────
    if num_media > 0:
        resp.message(MMS_MSG)
        return str(resp), 200

    body_upper = body.upper().strip()

    # ── UNREGISTERED USER ─────────────────────────────────────────
    user = get_or_create_user(from_number)
    user_id   = user["id"]
    user_tier = user.get("tier","unregistered")
    user_goal = user.get("goals",{}).get("goal_description","")

    if user_tier == "unregistered":
        step = user.get("onboarding_step", 1)
        if step == 1:
            # First ever message
            db.collection("users").document(user_id).update({"onboarding_step": 2})
            resp.message(WELCOME_MSG)
        else:
            # Second message — they came back, treat as signed up for demo
            # In production this checks Firebase Auth for completed signup
            db.collection("users").document(user_id).update({
                "tier": "coach",
                "onboarding_step": 3
            })
            resp.message(SECOND_MSG)
        return str(resp), 200

    # ── REGISTERED USER COMMANDS ──────────────────────────────────

    if body_upper in ("HELP","?"):
        resp.message(HELP_MSG)
        return str(resp), 200

    if body_upper == "CONFIRM":
        pending = get_pending(user_id)
        if pending:
            save_log(user_id, pending["type"], pending["data"])
            clear_pending(user_id)
            msg = "✅ Saved to your FitLogs dashboard."
            if user_tier in ("coach","trainer"):
                msg += "\n\nAnything else on your mind?"
            resp.message(msg)
        else:
            resp.message("Nothing pending to confirm.")
        return str(resp), 200

    if body_upper == "DISCARD":
        clear_pending(user_id)
        resp.message("Entry discarded. Nothing saved.")
        return str(resp), 200

    if body_upper == "DOWNGRADE":
        db.collection("users").document(user_id).update({"tier": "track"})
        resp.message("✅ Switched to Track ($9.99/mo). AI coaching conversations are paused but all logging continues. Text UPGRADE to switch back anytime.")
        return str(resp), 200

    if body_upper == "UPGRADE":
        db.collection("users").document(user_id).update({"tier": "coach"})
        resp.message("✅ Upgraded to Coach ($19.99/mo). Real-time AI coaching is now active. What's your goal?")
        return str(resp), 200

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

    # ── STEPS ──────────────────────────────────────────────────────
    steps = parse_steps(body)
    if steps is not None:
        goal = user.get("goals",{}).get("daily_steps",10000)
        save_log(user_id, "steps", {"steps": steps, "goal": goal})
        msg = steps_msg(steps, goal)
        if user_tier in ("coach","trainer"):
            msg += "\n\nKeep it moving! 💪"
        resp.message(msg)
        return str(resp), 200

    # ── MULTI-LOG: sleep + water + weight + routines ───────────────
    logged = []
    emoji_map = {"strength":"🏋","cardio":"🫁","flexibility":"🧘",
                 "vitamins":"💊","mindfulness":"🧠","rest_day":"🛌"}

    s = parse_sleep(body)
    if s is not None:
        save_log(user_id, "sleep", {"hours": s})
        logged.append(f"😴 {s} hrs sleep")

    w = parse_water(body)
    if w:
        amt, unit = w
        save_log(user_id, "hydration", {"amount": amt, "unit": unit})
        logged.append(f"💧 {amt}{unit} water")

    wt = parse_weight(body)
    if wt:
        amt, unit = wt
        save_log(user_id, "weight", {"weight": amt, "unit": unit})
        logged.append(f"⚖️ {amt} {unit}")

    for r in parse_routines(body):
        save_log(user_id, "routine", {"routine_type": r, "raw_text": body})
        logged.append(f"{emoji_map.get(r,'✅')} {r.replace('_',' ').title()}")

    if logged:
        msg = "✅ Logged:\n" + "\n".join(logged)
        if user_tier in ("coach","trainer"):
            msg += "\n\nSolid consistency. How are you feeling after?"
        resp.message(msg)
        return str(resp), 200

    # ── COACH TIER: conversational AI ─────────────────────────────
    if user_tier in ("coach","trainer") and is_conversational(body):
        try:
            reply = coach_reply(user_id, body, user_goal)
            resp.message(reply)
        except Exception as e:
            resp.message(f"I'm here — try again in a moment.\n{str(e)[:60]}")
        return str(resp), 200

    # ── BODY CHECK-IN ──────────────────────────────────────────────
    if is_body_checkin(body):
        try:
            analysis = analyze_body_text(body, user_goal)
            set_pending(user_id, {
                "type": "body_checkin",
                "data": {
                    "observation": analysis.get("observation",""),
                    "progress_note": analysis.get("progress_note",""),
                    "recommendation": analysis.get("recommendation",""),
                    "sentiment": analysis.get("sentiment","neutral"),
                    "user_description": body,
                    "user_goal": user_goal,
                    "ai_analyzed": True
                }
            })
            resp.message(body_confirm_msg(analysis))
        except Exception as e:
            resp.message(f"Could not analyze check-in. Try again.\n{str(e)[:60]}")
        return str(resp), 200

    # ── FOOD ENTRY ─────────────────────────────────────────────────
    if is_food(body) or len(body.split()) >= 3:
        try:
            analysis = analyze_food_text(body)
            set_pending(user_id, {
                "type": "food",
                "data": {
                    "meal_description": analysis.get("meal_description",""),
                    "estimated_calories": analysis.get("estimated_calories",0),
                    "protein_g": analysis.get("protein_g",0),
                    "carbs_g": analysis.get("carbs_g",0),
                    "fat_g": analysis.get("fat_g",0),
                    "confidence": analysis.get("confidence","medium"),
                    "notes": analysis.get("notes",""),
                    "raw_text": body,
                    "ai_analyzed": True,
                    "user_edited": False
                }
            })
            resp.message(food_confirm_msg(analysis))
        except Exception as e:
            resp.message(f"Could not estimate nutrition. Try again.\n{str(e)[:60]}")
        return str(resp), 200

    # ── COACH TIER FALLBACK: route to AI coach ─────────────────────
    if user_tier in ("coach","trainer"):
        try:
            reply = coach_reply(user_id, body, user_goal)
            resp.message(reply)
        except Exception as e:
            resp.message(f"I'm here — try again in a moment.\n{str(e)[:60]}")
        return str(resp), 200

    # ── TRACK TIER FALLBACK: save as note ─────────────────────────
    if body:
        save_log(user_id, "note", {"text": body})
        resp.message("📝 Note saved.\n\nReply HELP for logging options.")
    else:
        resp.message(HELP_MSG)

    return str(resp), 200

# ── Dashboard endpoints ───────────────────────────────────────────

@app.route("/analyze/food", methods=["POST"])
def analyze_food_endpoint():
    try:
        data = request.get_json()
        desc = data.get("description","").strip()
        if not desc:
            return jsonify({"error":"No description"}), 400
        return jsonify({"success": True, "analysis": analyze_food_text(desc)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/coach/message", methods=["POST"])
def coach_message_endpoint():
    """Dashboard AI coach chat endpoint."""
    try:
        data      = request.get_json()
        user_id   = data.get("user_id","")
        message   = data.get("message","").strip()
        user_goal = data.get("user_goal","")
        if not user_id or not message:
            return jsonify({"error":"Missing user_id or message"}), 400
        reply = coach_reply(user_id, message, user_goal)
        return jsonify({"success": True, "reply": reply})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/log/confirm", methods=["POST"])
def confirm_log():
    try:
        data     = request.get_json()
        user_id  = data.get("user_id","")
        log_type = data.get("type","")
        log_data = data.get("data",{})
        if not user_id or not log_type:
            return jsonify({"error":"Missing user_id or type"}), 400
        log_id = save_log(user_id, log_type, {**log_data, "confirmed_via":"dashboard"})
        return jsonify({"success": True, "log_id": log_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "fitlogs-webhook",
        "version": "1.1-coach",
        "number": FITLOGS_NUMBER,
        "mms_enabled": False
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
