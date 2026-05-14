# fitlogs-webhook

SMS webhook backend for fitlogs.ai — receives inbound SMS/MMS from Twilio, processes with Claude Vision, stores text-only results in Firestore.

## Architecture

```
User texts 1-888-596-5467
    ↓
Twilio receives SMS/MMS
    ↓
POST /sms → this Flask app (Railway)
    ↓
If photo → fetch image bytes into memory
    ↓
Send bytes to Claude Vision API
    ↓
Delete image bytes immediately
    ↓
Store text analysis only → Firestore (fitlogs-prod)
    ↓
Reply to user via Twilio
```

**Images are never written to disk or stored. Only AI-generated text observations are saved.**

## Message types handled

| Input | Detection | Response |
|---|---|---|
| Food photo (MMS) | Any MMS without check-in keywords | Calorie + macro estimate, awaits CONFIRM |
| Body check-in (MMS) | MMS + keywords: check-in, progress, selfie, mirror | Qualitative progress observation, awaits CONFIRM |
| Steps | "steps 8000" or "walked 6000 steps" | Logged immediately, progress bar reply |
| Sleep | "7 hours sleep" or "slept 8hr" | Logged immediately |
| Water | "2L water" or "64oz water" | Logged immediately |
| Weight | "weight 185lbs" | Logged immediately |
| Routines | gym, cardio, yoga, vitamins, meditation, rest | Logged immediately |
| CONFIRM | After any AI analysis | Saves pending entry to Firestore |
| DISCARD | After any AI analysis | Clears pending, nothing saved |
| CALORIES 520 | After food photo analysis | Updates calorie estimate before confirm |
| Anything else | Fallback | Saved as personal note |

## Environment variables (set in Railway)

```
TWILIO_AUTH_TOKEN=
TWILIO_ACCOUNT_SID=
ANTHROPIC_API_KEY=
FIREBASE_CREDENTIALS_JSON={"type":"service_account",...}
VALIDATE_TWILIO=true
PORT=5000
```

## Local dev setup

```bash
git clone https://github.com/sinkorserve/fitlogs-webhook
cd fitlogs-webhook
pip install -r requirements.txt

# Set env vars
export TWILIO_AUTH_TOKEN=your_token
export TWILIO_ACCOUNT_SID=your_sid
export ANTHROPIC_API_KEY=your_key
export FIREBASE_CREDENTIALS_JSON=$(cat serviceAccountKey.json)
export VALIDATE_TWILIO=false

python sms_webhook.py
```

Then use ngrok to expose locally for Twilio testing:
```bash
ngrok http 5000
```
Point Twilio webhook to: `https://your-ngrok-url.ngrok.io/sms`

## Firestore collections

```
users/{user_id}/
    phone: string
    tier: "free" | "track" | "coach"
    goals: { daily_calories, daily_steps, goal_description }
    health_metrics: { nutrition, activity, body_composition, sleep, hydration, strength, recovery }

users/{user_id}/logs/{log_id}/
    type: "food" | "body_checkin" | "steps" | "sleep" | "hydration" | "weight" | "routine" | "note"
    timestamp: datetime
    source: "sms" | "dashboard"
    image_stored: false  (always false — images are never stored)
    ... type-specific fields

users/{user_id}/pending/confirmation/
    type: string
    data: dict
    created_at: datetime
```

## Deploy to Railway

1. Push this repo to GitHub (sinkorserve/fitlogs-webhook)
2. Create new Railway project → Deploy from GitHub repo
3. Add all environment variables in Railway dashboard
4. Railway auto-detects Procfile and deploys
5. Copy the Railway public URL
6. In Twilio console → Phone Numbers → 1-888-596-5467 → Messaging webhook → set to `https://your-railway-url/sms`
