"""
Smart Meeting Assistant — Main Application
==========================================
Extracts action items, decisions, and deadlines from raw meeting notes.
Integrates with Google Calendar, Gmail, and Firestore for automated follow-ups.

Google Services used:
  • Gemini 2.0 Flash  — AI analysis
  • Firebase Firestore — persistent meeting history
  • Google Calendar   — action-item events
  • Gmail             — formatted summary email
  • Google OAuth 2.0  — secure authentication
"""

import os
import re
import json
import uuid
import base64
import logging
import time
from datetime import datetime, timedelta
from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import Flask, render_template, request, jsonify, session
from dotenv import load_dotenv
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import firebase_admin
from firebase_admin import credentials, firestore

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# ── Flask App ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-in-prod")

# ── Security Headers (CSP, HSTS, etc.) ───────────────────────────────────────
@app.after_request
def set_security_headers(response):
    """Apply security headers to every response."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "   # inline JS in template
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self';"
    )
    # HSTS only in production
    if os.getenv("FLASK_ENV") == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ── In-Memory Rate Limiter ────────────────────────────────────────────────────
_rate_store: dict[str, list] = defaultdict(list)

def is_rate_limited(key: str, max_requests: int = 10, window_seconds: int = 60) -> bool:
    """
    Sliding-window rate limiter.

    Args:
        key: Client identifier (IP address).
        max_requests: Maximum allowed requests in the window.
        window_seconds: Time window in seconds.

    Returns:
        True if the client has exceeded the rate limit, False otherwise.
    """
    now = time.time()
    timestamps = _rate_store[key]
    # Remove timestamps outside the current window
    _rate_store[key] = [t for t in timestamps if now - t < window_seconds]
    if len(_rate_store[key]) >= max_requests:
        return True
    _rate_store[key].append(now)
    return False


# ── Gemini AI Setup ───────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        model_name="gemini-2.0-flash",
        generation_config=genai.GenerationConfig(
            temperature=0.2,          # Lower = more deterministic JSON
            top_p=0.95,
            max_output_tokens=2048,
        )
    )
    logger.info("Gemini 2.0 Flash configured successfully.")
else:
    model = None
    logger.warning("GEMINI_API_KEY not set. AI features disabled.")


# ── Firebase Setup ────────────────────────────────────────────────────────────
firebase_cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH", "firebase-credentials.json")
db = None
if os.path.exists(firebase_cred_path):
    try:
        cred = credentials.Certificate(firebase_cred_path)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("Firebase Firestore connected.")
    except Exception as exc:
        logger.error("Firebase init failed: %s", exc)
else:
    logger.warning("Firebase credentials not found. Firestore disabled.")


# ── Google OAuth Config ───────────────────────────────────────────────────────
GOOGLE_CLIENT_SECRETS = os.getenv("GOOGLE_CLIENT_SECRETS_FILE", "client_secrets.json")
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]
# Only allow insecure transport in development — never hardcode True
if os.getenv("FLASK_ENV", "development") == "development":
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"


# ── Helper: Sanitize Input ────────────────────────────────────────────────────
def sanitize_input(text: str, max_length: int = 10000) -> str:
    """
    Strip dangerous characters and enforce a maximum character limit.

    Args:
        text: Raw user input string.
        max_length: Maximum number of characters to allow.

    Returns:
        Sanitized string, or empty string for non-string input.
    """
    if not isinstance(text, str):
        return ""
    text = text.strip()
    text = re.sub(r"[<>]", "", text)   # Prevent HTML/XSS injection
    return text[:max_length]


# ── Helper: Parse Gemini Response ─────────────────────────────────────────────
def parse_meeting_analysis(raw: str) -> dict:
    """
    Parse JSON from the Gemini response, handling markdown code fences.

    Falls back to a safe default structure if JSON is invalid so the caller
    always receives a consistent dict shape.

    Args:
        raw: Raw text response from Gemini.

    Returns:
        Parsed dict, or a default fallback dict.
    """
    try:
        # Strip optional ```json ... ``` fences
        match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
        json_str = match.group(1) if match else raw.strip()
        return json.loads(json_str)
    except (json.JSONDecodeError, AttributeError) as exc:
        logger.warning("JSON parse failed (%s). Returning raw text as summary.", exc)
        return {
            "summary": raw,
            "action_items": [],
            "decisions": [],
            "deadlines": [],
            "attendees": [],
            "next_meeting": None,
        }


# ── AI: Analyse Meeting Notes ─────────────────────────────────────────────────
def analyse_meeting(notes: str) -> dict:
    """
    Send meeting notes to Gemini 2.0 Flash and extract structured data.

    The prompt enforces strict JSON output so parse_meeting_analysis can
    reliably deserialise the response.

    Args:
        notes: Sanitized meeting notes text.

    Returns:
        Dict with keys: summary, action_items, decisions, deadlines,
        attendees, next_meeting. Contains an 'error' key on failure.
    """
    if not model:
        return {
            "error": "Gemini API not configured. Set GEMINI_API_KEY in .env.",
            "summary": notes,
            "action_items": [],
            "decisions": [],
            "deadlines": [],
            "attendees": [],
            "next_meeting": None,
        }

    prompt = f"""You are an expert meeting analyst. Analyse the following meeting notes and extract structured information.

STRICT RULES:
- Return ONLY valid JSON — no markdown, no preamble, no explanation.
- Use null (not "null") for missing dates.
- Infer priorities: tasks with imminent deadlines or explicit urgency → high; standard tasks → medium; nice-to-haves → low.
- Normalise all dates to YYYY-MM-DD format. If a relative date is given (e.g. "Friday", "next week"), resolve it relative to today: {datetime.now().strftime('%Y-%m-%d')}.

JSON SCHEMA:
{{
  "summary": "2-3 sentence summary of the meeting",
  "action_items": [
    {{
      "task": "description of the task",
      "owner": "person responsible (or 'Team' if unclear)",
      "due_date": "YYYY-MM-DD or null",
      "priority": "high|medium|low"
    }}
  ],
  "decisions": ["Decision 1", "Decision 2"],
  "deadlines": [
    {{
      "item": "what is due",
      "date": "YYYY-MM-DD or null",
      "owner": "who is responsible"
    }}
  ],
  "attendees": ["Name1", "Name2"],
  "next_meeting": "YYYY-MM-DD HH:MM or null"
}}

MEETING NOTES:
{notes}"""

    try:
        response = model.generate_content(prompt)
        raw = response.text.strip()
        logger.info("Gemini analysis complete.")
        return parse_meeting_analysis(raw)
    except Exception as exc:
        logger.error("Gemini API error: %s", exc)
        return {
            "error": str(exc),
            "summary": "",
            "action_items": [],
            "decisions": [],
            "deadlines": [],
            "attendees": [],
            "next_meeting": None,
        }


# ── Firestore: Batched Write ──────────────────────────────────────────────────
def save_meeting_to_firestore(
    meeting_id: str,
    notes: str,
    analysis: dict,
    user_email: str = "anonymous",
) -> bool:
    """
    Save the meeting document to Firestore using a batched write for atomicity.

    Args:
        meeting_id: UUID for this meeting session.
        notes: Sanitized original notes.
        analysis: Structured Gemini analysis.
        user_email: Authenticated user email, or 'anonymous'.

    Returns:
        True on success, False on failure.
    """
    if not db:
        logger.warning("Firestore not available. Skipping save.")
        return False
    try:
        batch = db.batch()
        meeting_ref = db.collection("meetings").document(meeting_id)
        batch.set(meeting_ref, {
            "meeting_id": meeting_id,
            "user_email": user_email,
            "raw_notes": notes,
            "analysis": analysis,
            "created_at": firestore.SERVER_TIMESTAMP,
            "calendar_events_created": False,
            "calendar_event_ids": [],
            "email_sent": False,
        })
        batch.commit()
        logger.info("Meeting %s saved to Firestore.", meeting_id)
        return True
    except Exception as exc:
        logger.error("Firestore save failed: %s", exc)
        return False


def update_meeting_flags(meeting_id: str, updates: dict) -> None:
    """
    Atomically update meeting flags (e.g. calendar_events_created, email_sent).

    Args:
        meeting_id: Target document ID.
        updates: Field-value dict to merge into the document.
    """
    if not db:
        return
    try:
        db.collection("meetings").document(meeting_id).update(updates)
    except Exception as exc:
        logger.warning("Firestore flag update failed for %s: %s", meeting_id, exc)


# ── Google Calendar: Create Events ────────────────────────────────────────────
def create_calendar_events(
    credentials_dict: dict,
    action_items: list,
    meeting_summary: str,
) -> list[str]:
    """
    Create one Google Calendar all-day event per action item.

    High-priority items are coloured Tomato (red), medium Banana (yellow),
    low Sage (green) for at-a-glance visibility.

    Args:
        credentials_dict: OAuth2 credentials from session.
        action_items: List of action item dicts from Gemini analysis.
        meeting_summary: Short summary used in event descriptions.

    Returns:
        List of created Calendar event IDs.
    """
    PRIORITY_COLOUR = {"high": "11", "medium": "5", "low": "10"}
    created_ids: list[str] = []

    try:
        creds = Credentials(**credentials_dict)
        service = build("calendar", "v3", credentials=creds)

        for item in action_items:
            due_date = item.get("due_date")
            # Default to 3 days from now if no date was extracted
            if not due_date:
                due_date = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")

            priority = item.get("priority", "medium").lower()
            event_body = {
                "summary": f"[Action] {item.get('task', 'Meeting Task')}",
                "description": (
                    f"Owner: {item.get('owner', 'Team')}\n"
                    f"Priority: {priority.upper()}\n\n"
                    f"Meeting context: {meeting_summary}\n\n"
                    "Created by Smart Meeting Assistant."
                ),
                "start": {"date": due_date},
                "end": {"date": due_date},
                "colorId": PRIORITY_COLOUR.get(priority, "5"),
                "reminders": {
                    "useDefault": False,
                    "overrides": [
                        {"method": "email",  "minutes": 24 * 60},
                        {"method": "popup",  "minutes": 60},
                    ],
                },
            }

            result = service.events().insert(
                calendarId="primary", body=event_body
            ).execute()
            created_ids.append(result.get("id", ""))
            logger.info("Calendar event created: %s", result.get("id"))

    except Exception as exc:
        logger.error("Calendar event creation failed: %s", exc)

    return created_ids


# ── Gmail: Send Summary Email ─────────────────────────────────────────────────
def send_summary_email(
    credentials_dict: dict,
    to_email: str,
    analysis: dict,
    meeting_id: str,
) -> bool:
    """
    Send a polished HTML meeting summary to the authenticated user via Gmail.

    The email includes: a summary paragraph, a colour-coded action items
    table, and a list of decisions made.

    Args:
        credentials_dict: OAuth2 credentials from session.
        to_email: Recipient address.
        analysis: Structured Gemini analysis.
        meeting_id: Meeting UUID for reference.

    Returns:
        True on success, False on failure.
    """
    try:
        creds = Credentials(**credentials_dict)
        service = build("gmail", "v1", credentials=creds)

        PRIORITY_COLOUR = {"high": "#ef4444", "medium": "#f59e0b", "low": "#10b981"}

        # Build rows for the action items table
        rows_html = "".join(
            f"""<tr>
              <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{i}. {item.get('task','')}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{item.get('owner','Team')}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;font-family:monospace;">{item.get('due_date','TBD')}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;color:{PRIORITY_COLOUR.get(item.get('priority','medium'),'#6b7280')};font-weight:700;">{item.get('priority','medium').upper()}</td>
            </tr>"""
            for i, item in enumerate(analysis.get("action_items", []), 1)
        ) or "<tr><td colspan='4' style='padding:12px;text-align:center;color:#6b7280;'>No action items found.</td></tr>"

        decisions_html = "".join(
            f"<li style='margin-bottom:6px;'>{d}</li>"
            for d in analysis.get("decisions", [])
        ) or "<li style='color:#6b7280;'>No decisions recorded.</li>"

        html_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;color:#1f2937;background:#f9fafb;padding:24px;">
  <div style="background:#0f1f3d;padding:28px 32px;border-radius:12px 12px 0 0;">
    <h1 style="color:white;margin:0;font-size:22px;">📋 Meeting Summary</h1>
    <p style="color:#93c5fd;margin:6px 0 0;font-size:13px;">
      Meeting ID: {meeting_id[:8]}… &nbsp;|&nbsp; {datetime.now().strftime('%B %d, %Y')}
    </p>
  </div>
  <div style="background:white;padding:28px 32px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 12px 12px;">
    <h2 style="color:#0f1f3d;font-size:16px;">Summary</h2>
    <p style="line-height:1.75;color:#374151;">{analysis.get('summary','No summary available.')}</p>

    <h2 style="color:#0f1f3d;font-size:16px;margin-top:24px;">✅ Action Items</h2>
    <table style="width:100%;border-collapse:collapse;background:#f9fafb;border-radius:8px;overflow:hidden;font-size:13px;">
      <tr style="background:#0f1f3d;color:white;">
        <th style="padding:10px 12px;text-align:left;">Task</th>
        <th style="padding:10px 12px;text-align:left;">Owner</th>
        <th style="padding:10px 12px;text-align:left;">Due</th>
        <th style="padding:10px 12px;text-align:left;">Priority</th>
      </tr>
      {rows_html}
    </table>

    <h2 style="color:#0f1f3d;font-size:16px;margin-top:24px;">🎯 Decisions Made</h2>
    <ul style="background:#f9fafb;padding:16px 16px 16px 32px;border-radius:8px;border:1px solid #e5e7eb;">
      {decisions_html}
    </ul>

    <p style="color:#9ca3af;font-size:11px;margin-top:28px;text-align:center;border-top:1px solid #f3f4f6;padding-top:16px;">
      Sent by Smart Meeting Assistant &nbsp;·&nbsp; Calendar events created for all action items.
    </p>
  </div>
</body></html>"""

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"📋 Meeting Summary — {datetime.now().strftime('%B %d, %Y')}"
        msg["From"] = "me"
        msg["To"] = to_email
        msg.attach(MIMEText(html_body, "html"))

        raw_bytes = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw_bytes}).execute()
        logger.info("Summary email sent to %s", to_email)
        return True

    except Exception as exc:
        logger.error("Gmail send failed: %s", exc)
        return False


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Render the main UI page."""
    return render_template("index.html", user_email=session.get("user_email"))


@app.route("/analyse", methods=["POST"])
def analyse():
    """
    POST /analyse
    Accepts JSON body: { "notes": "<meeting notes>" }

    Pipeline:
      1. Rate-limit check (10 req/min per IP)
      2. Input sanitisation + length validation
      3. Gemini 2.0 Flash analysis
      4. Batched Firestore save
      5. Google Calendar event creation (if authenticated)
      6. Gmail summary email (if authenticated)
      7. Atomic Firestore flag updates

    Returns 400 for bad input, 429 for rate-limit, 500 for server errors,
    200 with JSON analysis on success.
    """
    client_ip = request.remote_addr or "unknown"
    if is_rate_limited(client_ip, max_requests=10, window_seconds=60):
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be valid JSON."}), 400

    notes = sanitize_input(data.get("notes", ""), max_length=10000)
    if not notes or len(notes) < 20:
        return jsonify({"error": "Please provide meeting notes (at least 20 characters)."}), 400

    meeting_id = str(uuid.uuid4())
    user_email = session.get("user_email", "anonymous")

    # Step 1 — AI Analysis
    analysis = analyse_meeting(notes)
    if "error" in analysis and not analysis.get("summary"):
        return jsonify({"error": analysis["error"]}), 500

    # Step 2 — Persist to Firestore (batched write)
    save_meeting_to_firestore(meeting_id, notes, analysis, user_email)

    # Step 3 — Calendar Events (authenticated users only)
    calendar_ids: list[str] = []
    if "google_credentials" in session and analysis.get("action_items"):
        calendar_ids = create_calendar_events(
            session["google_credentials"],
            analysis["action_items"],
            analysis.get("summary", ""),
        )
        if calendar_ids:
            update_meeting_flags(meeting_id, {
                "calendar_events_created": True,
                "calendar_event_ids": calendar_ids,
            })

    # Step 4 — Send Email (authenticated users only)
    email_sent = False
    if "google_credentials" in session and user_email != "anonymous":
        email_sent = send_summary_email(
            session["google_credentials"], user_email, analysis, meeting_id
        )
        if email_sent:
            update_meeting_flags(meeting_id, {"email_sent": True})

    return jsonify({
        "meeting_id": meeting_id,
        "analysis": analysis,
        "calendar_events_created": len(calendar_ids),
        "email_sent": email_sent,
        "authenticated": "google_credentials" in session,
    })


@app.route("/history")
def history():
    """
    GET /history
    Return the last 10 meetings for the current user from Firestore.
    """
    if not db:
        return jsonify({"meetings": [], "message": "Firestore not configured."})

    user_email = session.get("user_email", "anonymous")
    try:
        docs = (
            db.collection("meetings")
            .where("user_email", "==", user_email)
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(10)
            .stream()
        )
        meetings = [
            {
                "meeting_id": d.get("meeting_id", ""),
                "summary": d.get("analysis", {}).get("summary", ""),
                "action_count": len(d.get("analysis", {}).get("action_items", [])),
                "created_at": str(d.get("created_at", "")),
            }
            for doc in docs
            for d in [doc.to_dict()]
        ]
        return jsonify({"meetings": meetings})
    except Exception as exc:
        logger.error("Firestore history fetch failed: %s", exc)
        return jsonify({"meetings": [], "error": str(exc)})


@app.route("/meeting/<meeting_id>")
def get_meeting(meeting_id: str):
    """
    GET /meeting/<meeting_id>
    Retrieve a single meeting document by ID.
    """
    if not re.match(r"^[0-9a-f-]{36}$", meeting_id):
        return jsonify({"error": "Invalid meeting ID format."}), 400
    if not db:
        return jsonify({"error": "Firestore not configured."}), 503
    try:
        doc = db.collection("meetings").document(meeting_id).get()
        if not doc.exists:
            return jsonify({"error": "Meeting not found."}), 404
        data = doc.to_dict()
        # Restrict access: only owner or anonymous meetings
        if data.get("user_email") not in (session.get("user_email"), "anonymous"):
            return jsonify({"error": "Access denied."}), 403
        data["created_at"] = str(data.get("created_at", ""))
        return jsonify(data)
    except Exception as exc:
        logger.error("Firestore get_meeting failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/export/<meeting_id>")
def export_meeting(meeting_id: str):
    """
    GET /export/<meeting_id>
    Export a meeting analysis as a plain-text action plan.
    """
    if not re.match(r"^[0-9a-f-]{36}$", meeting_id):
        return jsonify({"error": "Invalid meeting ID format."}), 400
    if not db:
        return jsonify({"error": "Firestore not configured."}), 503
    try:
        doc = db.collection("meetings").document(meeting_id).get()
        if not doc.exists:
            return jsonify({"error": "Meeting not found."}), 404

        d = doc.to_dict()
        analysis = d.get("analysis", {})
        lines = [
            f"MEETING SUMMARY — {meeting_id[:8]}",
            "=" * 50,
            "",
            "SUMMARY",
            analysis.get("summary", "N/A"),
            "",
            "ACTION ITEMS",
        ]
        for item in analysis.get("action_items", []):
            lines.append(
                f"  [{item.get('priority','?').upper()}] {item.get('task','')} "
                f"— {item.get('owner','Team')} by {item.get('due_date','TBD')}"
            )
        lines += ["", "DECISIONS"]
        for dec in analysis.get("decisions", []):
            lines.append(f"  • {dec}")
        lines += ["", "ATTENDEES", "  " + ", ".join(analysis.get("attendees", []))]

        text = "\n".join(lines)
        from flask import Response
        return Response(
            text,
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename=meeting-{meeting_id[:8]}.txt"},
        )
    except Exception as exc:
        logger.error("Export failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/auth/google")
def google_auth():
    """GET /auth/google — Initiate Google OAuth2 authorisation flow."""
    if not os.path.exists(GOOGLE_CLIENT_SECRETS):
        return jsonify({"error": "Google OAuth not configured. Add client_secrets.json."}), 500

    flow = Flow.from_client_secrets_file(
        GOOGLE_CLIENT_SECRETS,
        scopes=SCOPES,
        redirect_uri=os.getenv("OAUTH_REDIRECT_URI", "http://localhost:5000/auth/callback"),
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    session["oauth_state"] = state
    return jsonify({"auth_url": auth_url})


@app.route("/auth/callback")
def google_callback():
    """GET /auth/callback — Handle Google OAuth2 redirect and store credentials."""
    if not os.path.exists(GOOGLE_CLIENT_SECRETS):
        return "OAuth not configured.", 500

    flow = Flow.from_client_secrets_file(
        GOOGLE_CLIENT_SECRETS,
        scopes=SCOPES,
        state=session.get("oauth_state"),
        redirect_uri=os.getenv("OAUTH_REDIRECT_URI", "http://localhost:5000/auth/callback"),
    )
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials

    session["google_credentials"] = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes) if creds.scopes else [],
    }

    try:
        user_info_service = build("oauth2", "v2", credentials=creds)
        user_info = user_info_service.userinfo().get().execute()
        session["user_email"] = user_info.get("email", "unknown")
        logger.info("User authenticated: %s", session["user_email"])
    except Exception as exc:
        logger.warning("Could not fetch user email: %s", exc)

    return render_template("callback.html")


@app.route("/auth/logout")
def logout():
    """GET /auth/logout — Clear session data."""
    session.clear()
    return jsonify({"success": True, "message": "Logged out successfully."})


@app.route("/health")
def health():
    """GET /health — Liveness and dependency status check."""
    return jsonify({
        "status": "healthy",
        "gemini": model is not None,
        "gemini_model": "" if model else None,
        "firestore": db is not None,
        "timestamp": datetime.now().isoformat(),
    })


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_ENV", "development") == "development"
    app.run(debug=debug_mode, host="0.0.0.0", port=5000)
