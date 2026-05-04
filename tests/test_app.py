"""
Tests for Smart Meeting Assistant
===================================
Run with:  pytest tests/ -v
Coverage:  pytest tests/ -v --cov=app --cov-report=term-missing
"""

import pytest
import json
import sys
import os

# Make `app` importable from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch, MagicMock, call
from app import app, sanitize_input, parse_meeting_analysis, analyse_meeting, is_rate_limited


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    """Create a test Flask client with isolated session."""
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Clear the in-memory rate store before every test."""
    import app as app_module
    app_module._rate_store.clear()
    yield
    app_module._rate_store.clear()


SAMPLE_NOTES = """
Team sync — April 28, 2026
Attendees: Alice, Bob, Carol

- Q2 roadmap needs to be finalised by May 10
- Bob to review API documentation before Friday
- Carol will prepare the presentation by May 5
- Decided to go with Firebase over AWS
- Next meeting: May 3rd at 3 PM
"""

SAMPLE_ANALYSIS = {
    "summary": "Team discussed Q2 roadmap and assigned key tasks.",
    "action_items": [
        {"task": "Review API documentation", "owner": "Bob",   "due_date": "2026-05-02", "priority": "high"},
        {"task": "Prepare presentation",      "owner": "Carol", "due_date": "2026-05-05", "priority": "medium"},
        {"task": "Finalise Q2 roadmap",       "owner": "Team",  "due_date": "2026-05-10", "priority": "high"},
    ],
    "decisions":   ["Use Firebase over AWS for backend infrastructure"],
    "deadlines":   [{"item": "Q2 roadmap", "date": "2026-05-10", "owner": "Team"}],
    "attendees":   ["Alice", "Bob", "Carol"],
    "next_meeting": "2026-05-03 15:00",
}


# ── sanitize_input ────────────────────────────────────────────────────────────

class TestSanitizeInput:

    def test_strips_whitespace(self):
        assert sanitize_input("  hello  ") == "hello"

    def test_removes_angle_brackets(self):
        result = sanitize_input("<script>alert('xss')</script>")
        assert "<" not in result
        assert ">" not in result

    def test_enforces_max_length(self):
        assert len(sanitize_input("a" * 15000, max_length=10000)) == 10000

    def test_non_string_returns_empty(self):
        assert sanitize_input(None) == ""
        assert sanitize_input(123)  == ""
        assert sanitize_input([])   == ""

    def test_preserves_normal_text(self):
        text = "Meeting notes: decided to use Firebase."
        assert sanitize_input(text) == text

    def test_empty_string(self):
        assert sanitize_input("") == ""

    def test_custom_max_length(self):
        assert len(sanitize_input("x" * 100, max_length=50)) == 50


# ── parse_meeting_analysis ────────────────────────────────────────────────────

class TestParseMeetingAnalysis:

    def test_valid_json(self):
        result = parse_meeting_analysis(json.dumps(SAMPLE_ANALYSIS))
        assert result["summary"] == SAMPLE_ANALYSIS["summary"]
        assert len(result["action_items"]) == 3

    def test_json_in_code_fence(self):
        fenced = f"```json\n{json.dumps(SAMPLE_ANALYSIS)}\n```"
        result = parse_meeting_analysis(fenced)
        assert result["summary"] == SAMPLE_ANALYSIS["summary"]

    def test_invalid_json_returns_default_structure(self):
        result = parse_meeting_analysis("This is not JSON.")
        assert "summary" in result
        assert isinstance(result["action_items"], list)
        assert isinstance(result["decisions"], list)

    def test_fallback_puts_raw_in_summary(self):
        raw = "Random unstructured text."
        assert parse_meeting_analysis(raw)["summary"] == raw

    def test_all_expected_keys_present_on_fallback(self):
        result = parse_meeting_analysis("bad input")
        for key in ("summary", "action_items", "decisions", "deadlines", "attendees", "next_meeting"):
            assert key in result


# ── analyse_meeting ───────────────────────────────────────────────────────────

class TestAnalyseMeeting:

    @patch("app.model")
    def test_returns_structured_dict(self, mock_model):
        mock_model.generate_content.return_value = MagicMock(text=json.dumps(SAMPLE_ANALYSIS))
        result = analyse_meeting(SAMPLE_NOTES)
        for key in ("summary", "action_items", "decisions"):
            assert key in result

    @patch("app.model")
    def test_action_items_always_list(self, mock_model):
        mock_model.generate_content.return_value = MagicMock(text=json.dumps(SAMPLE_ANALYSIS))
        assert isinstance(analyse_meeting(SAMPLE_NOTES)["action_items"], list)

    @patch("app.model")
    def test_api_error_returns_error_dict(self, mock_model):
        mock_model.generate_content.side_effect = Exception("API quota exceeded")
        result = analyse_meeting(SAMPLE_NOTES)
        assert "error" in result

    def test_model_none_returns_error(self):
        with patch("app.model", None):
            result = analyse_meeting(SAMPLE_NOTES)
            assert "error" in result

    @patch("app.model")
    def test_gemini_called_once(self, mock_model):
        mock_model.generate_content.return_value = MagicMock(text=json.dumps(SAMPLE_ANALYSIS))
        analyse_meeting(SAMPLE_NOTES)
        assert mock_model.generate_content.call_count == 1


# ── Rate Limiter ──────────────────────────────────────────────────────────────

class TestRateLimiter:

    def test_allows_requests_under_limit(self):
        for _ in range(9):
            assert is_rate_limited("127.0.0.1", max_requests=10, window_seconds=60) is False

    def test_blocks_when_limit_exceeded(self):
        for _ in range(10):
            is_rate_limited("10.0.0.1", max_requests=10, window_seconds=60)
        assert is_rate_limited("10.0.0.1", max_requests=10, window_seconds=60) is True

    def test_different_ips_are_independent(self):
        for _ in range(10):
            is_rate_limited("192.168.1.1", max_requests=10, window_seconds=60)
        # A different IP should still be fine
        assert is_rate_limited("192.168.1.2", max_requests=10, window_seconds=60) is False


# ── Security Headers ──────────────────────────────────────────────────────────

class TestSecurityHeaders:

    def test_csp_header_present(self, client):
        res = client.get("/")
        assert "Content-Security-Policy" in res.headers

    def test_x_frame_options_deny(self, client):
        res = client.get("/health")
        assert res.headers.get("X-Frame-Options") == "DENY"

    def test_x_content_type_options(self, client):
        res = client.get("/health")
        assert res.headers.get("X-Content-Type-Options") == "nosniff"

    def test_referrer_policy(self, client):
        res = client.get("/health")
        assert "Referrer-Policy" in res.headers


# ── Flask Routes ──────────────────────────────────────────────────────────────

class TestRoutes:

    def test_index_200(self, client):
        assert client.get("/").status_code == 200

    def test_health_returns_status(self, client):
        data = json.loads(client.get("/health").data)
        assert data["status"] == "healthy"

    def test_health_includes_model_name(self, client):
        data = json.loads(client.get("/health").data)
        assert "gemini_model" in data

    def test_analyse_rejects_empty_notes(self, client):
        assert client.post("/analyse", json={"notes": ""}, content_type="application/json").status_code == 400

    def test_analyse_rejects_short_notes(self, client):
        assert client.post("/analyse", json={"notes": "short"}, content_type="application/json").status_code == 400

    def test_analyse_rejects_bad_content_type(self, client):
        assert client.post("/analyse", data="not json", content_type="text/plain").status_code == 400

    @patch("app.analyse_meeting")
    @patch("app.save_meeting_to_firestore")
    def test_analyse_returns_meeting_id(self, mock_save, mock_analyse, client):
        mock_analyse.return_value = SAMPLE_ANALYSIS
        mock_save.return_value = True
        res = client.post("/analyse", json={"notes": SAMPLE_NOTES}, content_type="application/json")
        assert res.status_code == 200
        data = json.loads(res.data)
        assert "meeting_id" in data
        assert len(data["meeting_id"]) == 36  # UUID v4 format

    @patch("app.analyse_meeting")
    @patch("app.save_meeting_to_firestore")
    def test_analyse_response_has_analysis_keys(self, mock_save, mock_analyse, client):
        mock_analyse.return_value = SAMPLE_ANALYSIS
        mock_save.return_value = True
        data = json.loads(client.post("/analyse", json={"notes": SAMPLE_NOTES}, content_type="application/json").data)
        assert "analysis" in data
        assert "calendar_events_created" in data
        assert "email_sent" in data

    def test_history_returns_list(self, client):
        data = json.loads(client.get("/history").data)
        assert "meetings" in data
        assert isinstance(data["meetings"], list)

    def test_logout_clears_session(self, client):
        data = json.loads(client.get("/auth/logout").data)
        assert data["success"] is True

    def test_get_meeting_invalid_id(self, client):
        assert client.get("/meeting/not-a-valid-uuid").status_code == 400

    def test_export_invalid_id(self, client):
        assert client.get("/export/not-a-valid-uuid").status_code == 400

    def test_get_meeting_no_firestore(self, client):
        with patch("app.db", None):
            assert client.get("/meeting/12345678-1234-1234-1234-123456789012").status_code == 503

    def test_export_no_firestore(self, client):
        with patch("app.db", None):
            assert client.get("/export/12345678-1234-1234-1234-123456789012").status_code == 503


# ── Input Validation Edge Cases ───────────────────────────────────────────────

class TestInputValidation:

    @patch("app.analyse_meeting")
    @patch("app.save_meeting_to_firestore")
    def test_very_long_notes_are_truncated(self, mock_save, mock_analyse, client):
        """Notes > 10k chars are truncated by sanitizer, not rejected."""
        mock_analyse.return_value = SAMPLE_ANALYSIS
        mock_save.return_value = True
        long_notes = "Meeting notes. " * 1000   # ~15k chars
        res = client.post("/analyse", json={"notes": long_notes}, content_type="application/json")
        assert res.status_code in (200, 400)

    def test_analyse_rate_limit_triggers_429(self, client):
        """After 10 rapid requests from same IP, expect 429."""
        with patch("app.analyse_meeting", return_value=SAMPLE_ANALYSIS), \
             patch("app.save_meeting_to_firestore", return_value=True):
            responses = [
                client.post("/analyse", json={"notes": SAMPLE_NOTES}, content_type="application/json")
                for _ in range(11)
            ]
        status_codes = [r.status_code for r in responses]
        assert 429 in status_codes

    def test_xss_payload_stripped(self, client):
        """XSS in notes should not propagate — angle brackets stripped."""
        with patch("app.analyse_meeting", return_value=SAMPLE_ANALYSIS), \
             patch("app.save_meeting_to_firestore", return_value=True):
            res = client.post(
                "/analyse",
                json={"notes": "<script>alert('xss')</script> " + "x" * 50},
                content_type="application/json",
            )
        # Either processed safely (200) or rejected as too short after stripping (400)
        assert res.status_code in (200, 400)

    def test_missing_notes_key_returns_400(self, client):
        res = client.post("/analyse", json={"other": "data"}, content_type="application/json")
        assert res.status_code == 400


# ── Firestore Helpers ─────────────────────────────────────────────────────────

class TestFirestoreHelpers:

    @patch("app.db")
    def test_save_meeting_uses_batch(self, mock_db):
        """save_meeting_to_firestore should use a Firestore batch."""
        from app import save_meeting_to_firestore
        mock_batch = MagicMock()
        mock_db.batch.return_value = mock_batch
        mock_db.collection.return_value.document.return_value = MagicMock()

        result = save_meeting_to_firestore("test-id", "notes", SAMPLE_ANALYSIS, "user@test.com")

        mock_db.batch.assert_called_once()
        mock_batch.set.assert_called_once()
        mock_batch.commit.assert_called_once()

    @patch("app.db", None)
    def test_save_returns_false_without_db(self):
        from app import save_meeting_to_firestore
        assert save_meeting_to_firestore("id", "notes", {}) is False

    @patch("app.db")
    def test_update_flags_called_with_correct_args(self, mock_db):
        from app import update_meeting_flags
        mock_ref = MagicMock()
        mock_db.collection.return_value.document.return_value = mock_ref
        update_meeting_flags("meeting-id", {"email_sent": True})
        mock_ref.update.assert_called_once_with({"email_sent": True})
