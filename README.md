# 📋 Smart Meeting Assistant

> Transform raw meeting notes into structured action plans — automatically.

[\*\*Python 3.11\*\* |(https://img.shields.io/badge/Python-3.11-blue?style=flat-square)](https://python.org)
[!\[Flask](https://img.shields.io/badge/Flask-3.0-green?style=flat-square)](https://flask.palletsprojects.com)
[!\[Gemini](https://img.shields.io/badge/gemini-2.0-flash-orange?style=flat-square)](https://ai.google.dev)
[!\[Firebase](https://img.shields.io/badge/Firebase-Firestore-yellow?style=flat-square)](https://firebase.google.com)
[!\[License](https://img.shields.io/badge/License-MIT-purple?style=flat-square)](LICENSE)

\---

## 🎯 Chosen Vertical

**Productivity / Workplace Automation**

Persona: Professionals, team leads, and project managers who spend significant time in meetings but struggle to track action **items**, follow through on decisions, and keep teams accountable.

\---

## 🚨 Problem Statement

The average professional spends **31 hours per month** in unproductive meetings. The core problem isn't the meetings themselves — it's what happens (or doesn't happen) after:

* Action items get lost in scattered notes
* Nobody remembers who was responsible for what
* Deadlines slip because there are no reminders
* Decisions made in meetings are never formally recorded
* Writing and sending meeting summaries takes 20–30 minutes per meeting

Smart Meeting Assistant solves all of this in under 10 seconds.

\---

## ✨ How It Works

```
Paste Meeting Notes
        ↓
  Gemini AI Analysis
        ↓
 ┌──────────────────────────────────┐
 │  • Meeting summary               │
 │  • Action items with owners      │
 │  • Decisions recorded            │
 │  • Deadlines extracted           │
 └──────────────────────────────────┘
        ↓
 \[If Google connected]
        ↓
 Google Calendar Events Created ──── 📅 One per action item
        +
 Gmail Summary Sent ──────────────── 📧 Formatted HTML email
        +
 Firestore Record Saved ─────────── ☁️ Full history stored
```

\---

## 🔧 Google Services Used

|Service|How It's Used|
|-|-|
|**Gemini 2.0 Flash**|Core AI — extracts structure from raw meeting notes|
|**Firebase Firestore**|Stores every meeting session with full analysis data|
|**Google Calendar API**|Creates calendar events for every action item with due dates and reminders|
|**Gmail API**|Sends a beautifully formatted HTML meeting summary to the user|
|**Google OAuth 2.0**|Secure authentication — no passwords stored|

\---

## 🚀 Setup \& Installation

### Prerequisites

* Python 3.11+
* A Google Cloud project with Calendar API and Gmail API enabled
* A Firebase project with Firestore enabled
* A Gemini API key from [Google AI Studio](https://aistudio.google.com)

### 1\. Clone the repository

```bash
git clone https://github.com/chittideepthi336/smart-meeting-assistant.git
cd smart-meeting-assistant
```

### 2\. Install dependencies

```bash
pip install -r requirements.txt
```

### 3\. Configure environment variables

```bash
cp .env.example .env
# Edit .env with your actual API keys
```

### 4\. Add credential files

Place these files in the project root (they are gitignored):

* `firebase-credentials.json` — Firebase service account key
* `client\_secrets.json` — Google OAuth 2.0 client credentials

### 5\. Run the app

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000) in your browser.

\---

## 📁 Project Structure

```
smart-meeting-assistant/
├── app.py                  # Main Flask application
├── templates/
│   ├── index.html          # Main UI
│   └── callback.html       # OAuth callback page
├── tests/
│   ├── \_\_init\_\_.py
│   └── test\_app.py         # 20+ unit tests
├── requirements.txt        # Pinned dependencies
├── Dockerfile              # Container configuration
├── .env.example            # Environment variable template
├── .gitignore              # Excludes secrets and venv
└── README.md               # This file
```

\---

## 🧪 Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run with coverage report
pytest tests/ -v --cov=app --cov-report=term-missing
```

Tests cover:

* Input sanitization and XSS prevention
* JSON parsing with valid and invalid inputs
* Gemini API error handling
* All Flask routes (200, 400, 500 cases)
* Edge cases: empty input, missing body, oversized input

\---

## 🏗 Architecture \& Logic

### AI Analysis Pipeline

1. User submits meeting notes via POST `/analyse`
2. Input is sanitized (max 10,000 chars, HTML stripped)
3. A structured prompt is sent to Gemini 2.0 Flash
4. Response is parsed — JSON extracted from markdown fences if needed
5. Fallback structure returned if parsing fails

### Google Calendar Integration

* Creates one all-day event per action item
* High priority items are colour-coded red
* Each event includes 24-hour email + 1-hour popup reminder
* Falls back to 3 days from now if no due date is specified

### Gmail Integration

* Sends an HTML-formatted summary email
* Includes action items table with priority colour coding
* Lists all decisions made
* Professional styling with the user's meeting ID for reference

### Firestore Data Model

```
meetings/
  {meeting\_id}/
    user\_email: string
    raw\_notes: string
    analysis: {
      summary: string
      action\_items: \[{task, owner, due\_date, priority}]
      decisions: \[string]
      deadlines: \[{item, date, owner}]
      attendees: \[string]
      next\_meeting: string | null
    }
    created\_at: timestamp
    calendar\_events\_created: boolean
    email\_sent: boolean
```

\---

## 🔒 Security

* All user input is sanitized before processing (XSS prevention)
* API keys loaded from environment variables — never hardcoded
* Credential files excluded via `.gitignore`
* Non-root Docker user for container security
* OAuth 2.0 used for all Google service authentication
* Input length enforced at 10,000 characters maximum

\---

## ♿ Accessibility

* All interactive elements have `aria-label` attributes
* Form inputs have associated `<label>` elements
* Live regions (`aria-live`) for dynamic content updates
* Keyboard navigation support for all interactive elements
* Screen-reader-only utility class for descriptive text
* Colour is never the sole indicator of meaning (priority shown as text + colour)
* Sufficient colour contrast ratios throughout the UI

\---

## 🧩 Assumptions Made

1. Meeting notes are in English (Gemini works best with English input)
2. Due dates in notes are in a format Gemini can recognise (e.g., "May 10", "Friday", "next week")
3. The Google Calendar and Gmail APIs are enabled in the user's Google Cloud project
4. Firebase Firestore is in Native mode (not Datastore mode)
5. The app runs on localhost:5000 during development — redirect URIs must be updated for production deployment

\---

## 🔮 Future Improvements

* \[ ] Audio/video transcript upload support
* \[ ] Multi-language meeting notes support
* \[ ] Slack integration for action item notifications
* \[ ] Weekly digest email of all pending action items
* \[ ] Team dashboard for shared meeting history
* \[ ] Google Meet integration for automatic transcript capture

\---

## 📄 License

MIT License — free to use and modify.

\---

*Built for Google PromptWars x Ascent Hackathon 2026*

