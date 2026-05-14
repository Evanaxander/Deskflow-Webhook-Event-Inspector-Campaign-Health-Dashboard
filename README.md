# Deskflow — Webhook Event Inspector & Campaign Health Dashboard

Real-time dashboard that receives, validates, and visualises webhook events
from Customer.io, Appcues, and LinkedIn campaign integrations.

Demo Video : https://youtu.be/BaL3mQlGas8?si=BAf_CfbtgM-vnuhk

<img width="792" height="814" alt="image" src="https://github.com/user-attachments/assets/ade2e209-95e2-4dd2-88b6-711cdc5fc8f2" />


## Project Structure

```
webhook-dashboard/
├── app.py              # Flask backend — all routes and logic
├── requirements.txt
├── events.db           # SQLite database (auto-created on first run)
├── templates/
│   └── index.html      # Single-page dashboard
└── README.md
```

## Setup & Run

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000

No API keys needed. No `.env` file needed.

## How It Works

### Webhook Receiver (POST /webhook)
Accepts JSON payloads. Validates:
- Required fields: `source`, `event_type`, `campaign`
- Source must be: `Customer.io`, `Appcues`, or `LinkedIn`
- Event type must match the source's allowed list

Valid events are stored in SQLite. Invalid ones return 4xx with a reason.

### Dashboard (/api/stats)
Returns all data in one call — polled every 3 seconds by the frontend:
- Total events by source
- Total events by event type
- Hourly time-series for last 24 hours (missing hours filled with 0)
- Campaign health status per source
- 20 most recent events

### Campaign Health
Each source has a configurable threshold (events/hour for a key event type):

| Source      | Key Event      | Default Threshold |
|-------------|----------------|-------------------|
| Customer.io | email_opened   | 5/hour            |
| Appcues     | tour_completed | 3/hour            |
| LinkedIn    | ad_clicked     | 2/hour            |

If count in last hour < threshold → Warning. Thresholds are adjustable in the UI.

### Simulator (/api/simulate)
Sends N fake webhook events to the receiver in a background thread with a slight
delay between each so they appear live in the dashboard. No external tools needed.

## Validation Error Examples

```bash
# Missing field
curl -X POST http://localhost:5000/webhook \
  -H "Content-Type: application/json" \
  -d '{"source": "Customer.io"}'
# → 422: Missing required field: 'event_type'

# Wrong source
curl -X POST http://localhost:5000/webhook \
  -H "Content-Type: application/json" \
  -d '{"source": "Mailchimp", "event_type": "email_opened", "campaign": "test"}'
# → 422: Unknown source 'Mailchimp'

# Valid event
curl -X POST http://localhost:5000/webhook \
  -H "Content-Type: application/json" \
  -d '{"source": "Customer.io", "event_type": "email_opened", "campaign": "onboarding"}'
# → 200: {"status": "received", "event_type": "email_opened"}
```
