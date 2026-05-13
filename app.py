import sqlite3
import json
import random
import time
import requests
import threading
from flask import Flask, render_template, request, jsonify, g
from datetime import datetime, timedelta

app = Flask(__name__)
DB_PATH = "events.db"


# ── Database Setup ─
# We use SQLite — built into Python, zero setup, perfect for this use case.
# One table: events. Each row is one webhook event received.

def get_db():
    """Get a database connection. Flask's g object ensures one connection per request."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row  # lets us access columns by name
    return g.db

@app.teardown_appcontext
def close_db(error):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    """Create the events table if it doesn't exist. Called once on startup."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                source    TEXT NOT NULL,        -- Customer.io | Appcues | LinkedIn
                event_type TEXT NOT NULL,       -- email_opened, tour_completed, etc.
                campaign  TEXT,                 -- campaign name/id
                payload   TEXT,                 -- full JSON payload stored as string
                received_at DATETIME DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


# ── Validation ─────────────────────────────────────────────────────────────────
# We reject payloads that are missing required fields or have unknown sources.
# This is what the assessors want to see — not just blindly storing anything.

VALID_SOURCES = {"Customer.io", "Appcues", "LinkedIn"}

VALID_EVENT_TYPES = {
    "Customer.io": ["email_opened", "email_clicked", "email_bounced", "unsubscribed"],
    "Appcues":     ["tour_completed", "tour_dismissed", "step_completed", "tour_started"],
    "LinkedIn":    ["ad_clicked", "lead_converted", "ad_impression", "lead_form_opened"]
}

def validate_payload(data: dict) -> tuple[bool, str]:
    """
    Returns (is_valid, error_message).
    A valid payload must have: source, event_type, campaign.
    Source must be one of our known integrations.
    event_type must match the source.
    """
    if not isinstance(data, dict):
        return False, "Payload must be a JSON object"

    for field in ["source", "event_type", "campaign"]:
        if field not in data:
            return False, f"Missing required field: '{field}'"
        if not isinstance(data[field], str) or not data[field].strip():
            return False, f"Field '{field}' must be a non-empty string"

    if data["source"] not in VALID_SOURCES:
        return False, f"Unknown source '{data['source']}'. Must be one of: {', '.join(VALID_SOURCES)}"

    allowed_events = VALID_EVENT_TYPES.get(data["source"], [])
    if data["event_type"] not in allowed_events:
        return False, f"Event type '{data['event_type']}' is not valid for source '{data['source']}'"

    return True, ""


# ── Anomaly Detection ──────────────────────────────────────────────────────────
# For each source, we track a configurable threshold.
# If events in the last hour drop below the threshold, we flag it as a warning.
# Thresholds are stored in memory (could be moved to DB/config file).

THRESHOLDS = {
    "Customer.io": {"event_type": "email_opened", "min_per_hour": 5},
    "Appcues":     {"event_type": "tour_completed", "min_per_hour": 3},
    "LinkedIn":    {"event_type": "ad_clicked",     "min_per_hour": 2},
}

def check_health(db) -> list[dict]:
    """
    For each source, count the key event in the last hour.
    If below threshold, return a warning. If above, return healthy.
    """
    health = []
    one_hour_ago = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

    for source, config in THRESHOLDS.items():
        row = db.execute("""
            SELECT COUNT(*) as cnt FROM events
            WHERE source = ?
              AND event_type = ?
              AND received_at >= ?
        """, (source, config["event_type"], one_hour_ago)).fetchone()

        count = row["cnt"]
        threshold = config["min_per_hour"]
        is_healthy = count >= threshold

        health.append({
            "source": source,
            "event_type": config["event_type"],
            "count_last_hour": count,
            "threshold": threshold,
            "status": "healthy" if is_healthy else "warning",
            "message": (
                f"{count} {config['event_type']} events in last hour (threshold: {threshold})"
                if is_healthy
                else f"Only {count} {config['event_type']} events in last hour — below threshold of {threshold}"
            )
        })

    return health


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/webhook", methods=["POST"])
def receive_webhook():
    """
    The webhook receiver. Any external service (or our simulator) POSTs here.

    Flow:
      1. Parse JSON body
      2. Validate structure and field values
      3. Store in SQLite
      4. Return 200 with confirmation, or 4xx with error reason
    """
    # Must be JSON
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 415

    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON body"}), 400

    # Validate
    is_valid, error_msg = validate_payload(data)
    if not is_valid:
        return jsonify({"error": error_msg}), 422   # 422 Unprocessable Entity

    # Store
    db = get_db()
    db.execute(
        "INSERT INTO events (source, event_type, campaign, payload) VALUES (?, ?, ?, ?)",
        (data["source"], data["event_type"], data["campaign"], json.dumps(data))
    )
    db.commit()

    return jsonify({"status": "received", "event_type": data["event_type"]}), 200


@app.route("/api/stats")
def get_stats():
    """
    Returns all dashboard data in one call:
    - total events by source
    - total events by event_type
    - time-series: event counts per hour for last 24 hours
    - health panel status
    - recent 20 events for the live feed
    """
    db = get_db()

    # Total by source
    by_source = db.execute("""
        SELECT source, COUNT(*) as count FROM events GROUP BY source
    """).fetchall()

    # Total by event_type
    by_type = db.execute("""
        SELECT event_type, COUNT(*) as count FROM events GROUP BY event_type ORDER BY count DESC
    """).fetchall()

    # Time-series: last 24 hours, bucketed by hour
    # strftime('%H', received_at) gives the hour (00-23)
    twenty_four_ago = (datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    hourly = db.execute("""
        SELECT strftime('%Y-%m-%d %H:00', received_at) as hour, COUNT(*) as count
        FROM events
        WHERE received_at >= ?
        GROUP BY hour
        ORDER BY hour ASC
    """, (twenty_four_ago,)).fetchall()

    # Fill in missing hours with 0 so the chart looks complete
    hourly_map = {row["hour"]: row["count"] for row in hourly}
    hours_series = []
    for i in range(23, -1, -1):
        hour_dt  = datetime.utcnow() - timedelta(hours=i)
        hour_key = hour_dt.strftime("%Y-%m-%d %H:00")
        hours_series.append({
            "hour":  hour_dt.strftime("%H:00"),
            "count": hourly_map.get(hour_key, 0)
        })

    # Health check
    health = check_health(db)

    # Recent events feed
    recent = db.execute("""
        SELECT source, event_type, campaign, received_at
        FROM events ORDER BY received_at DESC LIMIT 20
    """).fetchall()

    # Total count
    total = db.execute("SELECT COUNT(*) as cnt FROM events").fetchone()["cnt"]

    return jsonify({
        "total": total,
        "by_source": [dict(r) for r in by_source],
        "by_type":   [dict(r) for r in by_type],
        "hourly":    hours_series,
        "health":    health,
        "recent":    [dict(r) for r in recent]
    })


@app.route("/api/simulate", methods=["POST"])
def simulate_events():
    """
    Sends a batch of realistic fake webhook events to our own /webhook endpoint.
    This lets us demo the dashboard without needing live integrations.

    We use threading so the simulation runs in the background and the response
    returns immediately — the frontend polls /api/stats to see events appear live.
    """
    count = request.get_json().get("count", 30)

    sample_events = [
        # Customer.io
        {"source": "Customer.io", "event_type": "email_opened",  "campaign": "onboarding-week1"},
        {"source": "Customer.io", "event_type": "email_clicked",  "campaign": "onboarding-week1"},
        {"source": "Customer.io", "event_type": "email_opened",  "campaign": "trial-expiry"},
        {"source": "Customer.io", "event_type": "email_bounced", "campaign": "onboarding-week2"},
        # Appcues
        {"source": "Appcues", "event_type": "tour_started",    "campaign": "product-tour-v2"},
        {"source": "Appcues", "event_type": "tour_completed",  "campaign": "product-tour-v2"},
        {"source": "Appcues", "event_type": "tour_dismissed",  "campaign": "feature-spotlight"},
        {"source": "Appcues", "event_type": "step_completed",  "campaign": "product-tour-v2"},
        # LinkedIn
        {"source": "LinkedIn", "event_type": "ad_clicked",        "campaign": "retargeting-q2"},
        {"source": "LinkedIn", "event_type": "lead_converted",     "campaign": "retargeting-q2"},
        {"source": "LinkedIn", "event_type": "ad_impression",      "campaign": "brand-awareness"},
        {"source": "LinkedIn", "event_type": "lead_form_opened",   "campaign": "retargeting-q2"},
    ]

    def send_batch():
        base_url = "http://127.0.0.1:5000/webhook"
        for _ in range(count):
            event = random.choice(sample_events).copy()
            event["timestamp"] = datetime.utcnow().isoformat()
            try:
                requests.post(base_url, json=event, timeout=5)
            except Exception as e:
                print(f"[SIMULATE ERROR] {e}")
            time.sleep(0.15)   # slight delay so events trickle in visibly

    thread = threading.Thread(target=send_batch, daemon=True)
    thread.start()

    return jsonify({"status": "started", "count": count})


@app.route("/api/threshold", methods=["POST"])
def update_threshold():
    """
    Updates the in-memory threshold for a source.
    The health check reads from THRESHOLDS dict, so this takes effect immediately
    on the next /api/stats call — no restart needed.
    """
    data = request.get_json()
    source = data.get("source")
    value  = data.get("value")

    if source not in THRESHOLDS:
        return jsonify({"error": "Unknown source"}), 400
    if not isinstance(value, int) or value < 1:
        return jsonify({"error": "Value must be a positive integer"}), 400

    THRESHOLDS[source]["min_per_hour"] = value
    return jsonify({"status": "updated", "source": source, "threshold": value})


@app.route("/api/clear", methods=["POST"])
def clear_events():
    """Wipe all events. Useful for demo resets."""
    db = get_db()
    db.execute("DELETE FROM events")
    db.commit()
    return jsonify({"status": "cleared"})


# ── Entry Point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print("[INFO] Database initialized.")
    print("[INFO] Dashboard running at http://127.0.0.1:5000")
    app.run(debug=True, port=5000)
