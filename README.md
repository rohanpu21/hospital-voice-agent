# Hospital Voice Agent

Dirt-cheap, multilingual AI receptionist for Indian hospitals. Handles inbound calls in **Hindi, Odia, and English**, books appointments directly into **Google Calendar**, and costs roughly **₹1.20 per 5-minute call** (all-in).

Built for the Bhubaneswar/Odisha market but configurable for any Indian hospital with zero code changes.

---

## Architecture

```
Caller (phone)
     │
     ▼
Telephony Provider          ← Exotel / Plivo / CloudBharat SIP
(Exotel/Plivo)
     │
     │  HTTP webhook (WAV audio)
     ▼
┌─────────────────────────────────────────────────────────┐
│               FastAPI Server  (main.py)                 │
│                                                         │
│  POST /call/start  ──► generates greeting audio         │
│  POST /call/input  ──► processes each patient turn      │
│  POST /test/chat   ──► text-only testing (no phone)     │
└──────────┬──────────────────────────┬───────────────────┘
           │                          │
           ▼                          ▼
  ┌─────────────────┐      ┌───────────────────────┐
  │  Sarvam AI      │      │  Groq LLM             │
  │  (voice_service)│      │  (agent.py)           │
  │                 │      │                       │
  │  STT: saaras:v1 │      │  llama-3.3-70b        │
  │  TTS: bulbul:v1 │      │  with tool calling    │
  │                 │      │                       │
  │  ₹0.50/min audio│      │  ~₹0.04/call          │
  └─────────────────┘      └──────────┬────────────┘
                                      │
                              tool calls (2 tools)
                                      │
                           ┌──────────▼────────────┐
                           │  Google Calendar API  │
                           │  (calendar_service.py)│
                           │                       │
                           │  get_available_slots  │
                           │  book_appointment     │
                           │                       │
                           │  Free tier (no cost)  │
                           └───────────────────────┘
```

---

## Booking Flow (Step by Step)

The agent follows a strict 7-step flow to book an appointment:

```
Patient calls ──► Greeting in hospital's primary language
                         │
                         ▼
              STEP 1: Ask for patient's full name
                         │
                         ▼
              STEP 2: Ask which doctor / department
                         │
                         ▼
              STEP 3: Ask for preferred date
                       ('kal' → tomorrow, 'aaj' → today)
                         │
                         ▼
              STEP 4: Call get_available_slots tool
                       (fetches real Google Calendar freebusy)
                         │
                         ▼
              STEP 5: Offer top 2-3 available time slots to patient
                         │
                         ▼
              STEP 6: Patient picks a slot →
                       Call book_appointment tool
                       (creates event in Google Calendar)
                         │
                         ▼
              STEP 7: Read back confirmation
                       (name, doctor, date, time)
                         │
                         ▼
                    Call ends ✓
```

The LLM is given hard **GATE CONDITIONS** — it cannot say "confirmed" until the `book_appointment` tool returns `success: true`.

---

## Language Detection

Every patient message is auto-detected using a 4-tier logic in `services/language_engine.py`:

| Priority | Method | Example |
|----------|--------|---------|
| 1 | Unicode script (Odia: U+0B00–U+0B7F) | ଆପଣଙ୍କ ନାମ |
| 2 | Unicode script (Devanagari: U+0900–U+097F) | आपका नाम |
| 3 | Keyword matching | `darkara` → Odia, `chahiye` → Hindi |
| 4 | Latin character ratio | >70% Latin → English |
| fallback | Hospital's `primary_lang` from config | — |

The session language is updated **every turn** so the agent can follow mid-call language switches.

**Voice mapping:**

| Language | STT/TTS code | Sarvam voice |
|----------|-------------|--------------|
| Odia | `od-IN` | `pavithra` |
| Hindi | `hi-IN` | `meera` |
| English | `en-IN` | `sarita` |

---

## Multi-Tenant System

One codebase serves multiple hospitals. To add a new hospital, add a single JSON entry to `config/hospitals.json` — no code changes required.

```json
{
  "hospitals": {
    "your-hospital-001": {
      "hospital_id": "your-hospital-001",
      "hospital_name": "Your Hospital Name",
      "agent_name": "Priya",
      "primary_lang": "hindi",
      "fallback_lang": "english",
      "calendar_id": "your-calendar-id@group.calendar.google.com",
      "credentials_file": "config/your_hospital_credentials.json",
      "doctors": [
        {
          "id": "dr_xyz_001",
          "name": "Dr. Name",
          "department": "Cardiology",
          "calendar_email": "your-calendar-id@group.calendar.google.com"
        }
      ],
      "working_hours": { "start": "09:00", "end": "17:00" },
      "timezone": "Asia/Kolkata",
      "slot_duration_minutes": 30
    }
  }
}
```

Each hospital can have its own:
- Agent name and personality
- Primary + fallback language
- Google Calendar and service account credentials
- Doctor roster with departments
- Working hours and slot duration

---

## Project Structure

```
c:/call agent/
├── main.py                      # FastAPI app — all HTTP endpoints
├── requirements.txt             # Python dependencies
├── .env                         # API keys (never commit)
├── .env.example                 # Template for .env
├── SETUP.md                     # Full setup walkthrough
│
├── config/
│   ├── hospitals.json           # Per-hospital config (committed — no secrets)
│   ├── prompts.json             # System prompt templates
│   └── avlys_credentials.json  # Google Service Account key (NOT committed)
│
├── services/
│   ├── agent.py                 # Groq LLM orchestrator + tool-calling loop
│   ├── calendar_service.py      # Google Calendar: get slots + book appointment
│   ├── voice_service.py         # Sarvam AI: STT (audio→text) + TTS (text→audio)
│   └── language_engine.py      # Trilingual language detection
│
└── utils/
    ├── session_store.py         # In-memory call session management (with TTL)
    └── sms_service.py           # Fast2SMS appointment confirmations (optional)
```

---

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/your-username/hospital-voice-agent.git
cd hospital-voice-agent
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```
GROQ_API_KEY=gsk_...         # Get from console.groq.com
SARVAM_API_KEY=sk_...        # Get from sarvam.ai
AGENT_API_KEY=your-secret    # Protects webhook endpoints (optional but recommended)
PORT=8000
```

### 3. Set up Google Calendar

1. Create a Google Cloud project at [console.cloud.google.com](https://console.cloud.google.com)
2. Enable the **Google Calendar API**
3. Create a **Service Account** and download its JSON key
4. Save the JSON key as `config/your_hospital_credentials.json`
5. Open Google Calendar → Settings → Share the calendar → add the service account email with **"Make changes to events"** permission
6. Copy the **Calendar ID** (from Calendar settings → Integrate calendar) into `config/hospitals.json`

### 4. Configure your hospital

Edit `config/hospitals.json` — add your hospital's details (see Multi-Tenant System above).

### 5. Run the server

```bash
# Development
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Production
python main.py
```

---

## API Endpoints

### `POST /call/start`

Called by telephony provider when a new call comes in. Returns greeting audio.

**Request:**
```json
{
  "hospital_id": "aiims-bbsr-001",
  "caller_phone": "+919876543210"
}
```

**Response:**
```json
{
  "call_id": "uuid-generated-server-side",
  "text": "Namaskar! AIIMS Bhubaneswar — Asha speaking. Appointment darkara ki?",
  "lang": "od-IN",
  "audio_b64": "base64-encoded-WAV"
}
```

---

### `POST /call/input`

Called each time the patient finishes speaking (multipart form).

**Form fields:**
- `call_id` — from `/call/start` response
- `audio_file` — WAV audio (max 10MB), OR
- `text_input` — plain text (for testing)

**Response:**
```json
{
  "patient_said": "Mera naam Rohan hai",
  "text": "Thank you Rohan Ji. Kaunse doctor se milna hai?",
  "audio_b64": "base64-encoded-WAV",
  "lang": "hi-IN",
  "appointment_booked": false,
  "booking_details": null,
  "end_call": false
}
```

---

### `POST /test/chat`

Text-only endpoint for testing without a real phone — no audio needed.

**Request:**
```json
{
  "hospital_id": "aiims-bbsr-001",
  "call_id": "test-session-1",
  "message": "Dr. Das ke saath appointment chahiye"
}
```

---

### `GET /health`

Returns service status. No auth required.

```json
{
  "status": "ok",
  "hospitals_loaded": 2,
  "active_calls": 0,
  "services": {
    "groq": true,
    "sarvam": true
  }
}
```

---

### Authentication

Set `AGENT_API_KEY` in `.env` to protect endpoints. Pass it as:

```
Authorization: Bearer your-secret-key
```

Applies to: `/call/start`, `/call/input`, `/hospitals`

---

## How the LLM Agent Works

The agent in `services/agent.py` uses Groq's **llama-3.3-70b-versatile** with native tool calling:

1. The system prompt is built dynamically from the hospital's config (doctor list, working hours, language rules)
2. Two tools are registered: `get_available_slots` and `book_appointment`
3. An **agentic loop** runs up to 5 iterations per turn — the LLM can call tools and use their results before giving the final text response
4. Tool results (slot availability, booking confirmation) are fed back into the conversation
5. The final text response is sent to Sarvam TTS for audio

The LLM is explicitly instructed:
- Never say "confirmed" unless `book_appointment` returned `success: true`
- Never call `get_available_slots` without a confirmed date in `YYYY-MM-DD` format
- Keep every response under 15 words (minimizes voice latency and API cost)
- Mirror the patient's language exactly

---

## Cost Breakdown

| Service | Cost | Per 5-min call |
|---------|------|----------------|
| Groq LLM | ~$0.59/1M tokens | ~₹0.04 |
| Sarvam STT | ₹0.50/min audio | ~₹0.80 |
| Sarvam TTS | ₹0.50/min audio | ~₹0.40 |
| Google Calendar API | Free | ₹0 |
| **Total** | | **~₹1.24/call** |

Suggested pricing: ₹5,000–₹8,000/month per hospital for unlimited calls.

---

## Testing Without a Phone

Use the `/test/chat` endpoint to simulate a full conversation:

```bash
# Turn 1
curl -X POST http://localhost:8000/test/chat \
  -H "Content-Type: application/json" \
  -d '{"hospital_id": "aiims-bbsr-001", "call_id": "test-1", "message": "Dr. Das ke saath appointment chahiye"}'

# Turn 2
curl -X POST http://localhost:8000/test/chat \
  -H "Content-Type: application/json" \
  -d '{"hospital_id": "aiims-bbsr-001", "call_id": "test-1", "message": "Mera naam Rohan Purohit hai"}'

# Turn 3
curl -X POST http://localhost:8000/test/chat \
  -H "Content-Type: application/json" \
  -d '{"hospital_id": "aiims-bbsr-001", "call_id": "test-1", "message": "Kal ke liye"}'

# Turn 4 — picks a slot, triggers booking
curl -X POST http://localhost:8000/test/chat \
  -H "Content-Type: application/json" \
  -d '{"hospital_id": "aiims-bbsr-001", "call_id": "test-1", "message": "9 baje wala"}'
```

Or run the included test script:

```bash
python test_booking.py
```

---

## Telephony Integration

Connect with any Indian telephony provider that supports HTTP webhooks:

| Provider | Notes |
|----------|-------|
| **Exotel** | Recommended for India. Webhook URL in "Applet" settings |
| **Plivo** | PHLO flows with webhook nodes |
| **CloudBharat** | SIP trunk + webhook |

Set the following in your telephony dashboard:
- **Answer URL**: `https://your-server.com/call/start` (POST)
- **Speech URL**: `https://your-server.com/call/input` (POST, multipart with WAV)

---

## Production Deployment

```bash
# Install production server
pip install gunicorn

# Run with workers
gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
```

For multi-hospital at scale, replace the in-memory `SESSIONS` dict in `main.py` with Redis (using `utils/session_store.py` as the base).

---

## Security Notes

- `.env` and `config/*_credentials.json` are git-ignored — never commit them
- `AGENT_API_KEY` guards all webhook endpoints
- `call_id` is always generated server-side (clients cannot spoof sessions)
- Credentials file paths are validated to stay within the app directory (no path traversal)
- Audio uploads are capped at 10MB

---

## License

MIT
