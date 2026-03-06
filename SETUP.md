# Hospital Voice Agent — Setup Guide

## What You Built

A **dirt cheap** trilingual AI receptionist for Indian hospitals that:
- Answers calls in **Hindi, Odia, and English** (auto-detects and mirrors the patient)
- Books appointments directly into **Google Calendar**
- Sends **SMS confirmations** via Fast2SMS
- Costs **~₹1.20 per 5-minute call** (all-in)
- Can be customized for **any hospital** by editing `config/hospitals.json`

---

## Step 1: Get Your API Keys (Free/Cheap)

| Service | Link | Cost |
|---------|------|------|
| Groq (LLM) | https://console.groq.com | Free / ₹0.05 per 1000 calls |
| Sarvam AI (Voice) | https://sarvam.ai | ₹30/hour of audio |
| Fast2SMS (SMS) | https://fast2sms.com | ₹0.20/SMS (optional) |

---

## Step 2: Google Calendar Service Account

1. Go to https://console.cloud.google.com
2. Create a new project (e.g. `hospital-agent`)
3. Enable **Google Calendar API**
4. Go to **IAM & Admin → Service Accounts → Create Service Account**
   - Name: `hospital-bot`
   - Role: Editor
5. Click the service account → **Keys → Add Key → JSON**
6. Save the downloaded file as `config/aiims_bbsr_credentials.json`
7. Open Google Calendar → Share calendar with the service account email
   - Permission: **"Make changes to events"**

---

## Step 3: Environment Setup

```bash
# 1. Clone or navigate to the project
cd "c:/call agent"

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment variables
cp .env.example .env
# Edit .env with your actual API keys
```

---

## Step 4: Configure Your Hospitals

Edit `config/hospitals.json` — add one entry per hospital:

```json
{
  "hospitals": {
    "your-hospital-id": {
      "hospital_id": "your-hospital-id",
      "hospital_name": "Your Hospital Name",
      "agent_name": "Asha",
      "primary_lang": "odia",          // or "hindi"
      "calendar_id": "calendar@email.com",
      "credentials_file": "config/your_credentials.json",
      "doctors": [
        {
          "id": "dr_001",
          "name": "Dr. Name",
          "department": "Department",
          "calendar_email": "dr@hospital.com"
        }
      ],
      "working_hours": {"start": "09:00", "end": "17:00"},
      "slot_duration_minutes": 30
    }
  }
}
```

---

## Step 5: Run the Server

```bash
python main.py
# Server starts at http://localhost:8000
```

---

## Step 6: Test Without a Phone

```bash
# Test Odia booking request
curl -X POST http://localhost:8000/test/chat \
  -H "Content-Type: application/json" \
  -d '{
    "hospital_id": "aiims-bbsr-001",
    "call_id": "test-001",
    "message": "Namaskar, mote Dr. Das nka sahita appointment darkara"
  }'

# Test Hindi booking request
curl -X POST http://localhost:8000/test/chat \
  -H "Content-Type: application/json" \
  -d '{
    "hospital_id": "aiims-bbsr-001",
    "call_id": "test-002",
    "message": "Namaste, Dr. Das ke saath appointment chahiye kal ke liye"
  }'
```

---

## Step 7: Connect to a Real Phone Line

### Option A: Exotel (Easiest for India)
1. Sign up at exotel.com (₹2,000 starter)
2. Get an Indian virtual number
3. Set webhook URL to: `https://your-server.com/call/start`
4. Route calls by hospital ID using Exotel's AppletBuilder

### Option B: CloudBharat SIP (Cheapest)
1. Get a SIP trunk from cloudbharat.com
2. Deploy the server on Hetzner VPS (~₹400/month)
3. Use a SIP gateway (like LiveKit SIP) to connect the SIP call to your FastAPI webhook

---

## Sell to a New Hospital (10 minutes)

1. Get their Google Calendar credentials JSON → save to `config/`
2. Add their entry to `config/hospitals.json`
3. Restart server
4. Point their phone number's webhook to `/call/start?hospital_id=their-id`

**Done.** No code changes required.

---

## Cost Breakdown (Per Hospital, Per Month)

| Item | Cost |
|------|------|
| Hetzner VPS (shared across all hospitals) | ₹400 |
| Indian phone number (SIP DID) | ₹300 |
| Groq API (first 5000 calls/month) | Free |
| Sarvam AI (100 hours of voice) | ₹3,000 |
| Fast2SMS (500 appointment SMS) | ₹100 |
| **Total for 100 hours of active calls** | **~₹3,800** |

**Sell for:** ₹8,000/month flat fee → **₹4,200 profit per hospital**
With 10 hospitals on the same server: **₹42,000/month profit**
