"""Webhook notifications to n8n: secret header, retries, never blocking, and the workflow file itself."""
import http.server
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app import booking

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def receiver():
    """A tiny local webhook. `statuses` is the list of HTTP codes to answer with, in order."""
    got, statuses = [], []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            got.append({"headers": dict(self.headers), "body": body})
            code = statuses.pop(0) if statuses else 200
            self.send_response(code)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/webhook/clinic-events", got, statuses
    srv.shutdown()


def test_event_has_secret_header_and_envelope(receiver, monkeypatch):
    url, got, _ = receiver
    monkeypatch.setenv("EVENT_WEBHOOK_URL", url)
    monkeypatch.setenv("EVENT_WEBHOOK_SECRET", "s3cret")
    assert booking.notify("appointment.booked", {"name": "Jane"}, wait=True) == 200
    req = got[0]
    assert req["headers"]["X-Webhook-Secret"] == "s3cret"
    assert req["body"]["event"] == "appointment.booked" and req["body"]["data"] == {"name": "Jane"}
    assert len(req["body"]["id"]) == 32 and req["body"]["sent_at"]


def test_retries_when_n8n_errors_then_succeeds(receiver, monkeypatch):
    url, got, statuses = receiver
    monkeypatch.setenv("EVENT_WEBHOOK_URL", url)
    monkeypatch.setattr(booking.clock, "sleep", lambda s: None)
    statuses += [500, 502]
    assert booking.notify("handoff.created", {}, wait=True) == 200 and len(got) == 3
    assert len({r["body"]["id"] for r in got}) == 1              # same event id: n8n can drop duplicates


def test_rejection_is_not_retried(receiver, monkeypatch):
    url, got, statuses = receiver
    monkeypatch.setenv("EVENT_WEBHOOK_URL", url)
    statuses.append(403)
    assert booking.notify("appointment.booked", {}, wait=True) == 403 and len(got) == 1


def test_background_send_does_not_block_and_n8n_down_is_harmless(monkeypatch):
    monkeypatch.setenv("EVENT_WEBHOOK_URL", "http://127.0.0.1:9/nothing-here")
    monkeypatch.setattr(booking.clock, "sleep", lambda s: None)
    start = time.time()
    assert booking.notify("appointment.booked", {}) is None
    assert time.time() - start < 0.5
    assert booking.notify("appointment.booked", {}, wait=True) is None        # gave up quietly, no exception


def test_no_url_means_no_notification(monkeypatch):
    monkeypatch.delenv("EVENT_WEBHOOK_URL", raising=False)
    assert booking.notify("appointment.booked", {}, wait=True) is None


def test_test_command(receiver, monkeypatch, capsys):
    url, got, _ = receiver
    monkeypatch.setenv("EVENT_WEBHOOK_URL", url)
    from app import notify
    monkeypatch.setattr(notify, "load_dotenv", lambda: None)
    assert notify.main(["handoff"]) == 0 and got[0]["body"]["event"] == "handoff.created"
    assert "HTTP 200 (OK)" in capsys.readouterr().out


def test_workflow_file_is_current_and_wired_up(tmp_path):
    committed = (ROOT / "automation" / "n8n_clinic_events.json").read_text(encoding="utf-8")
    subprocess.run([sys.executable, str(ROOT / "automation" / "build_workflow.py")], check=True, capture_output=True)
    assert (ROOT / "automation" / "n8n_clinic_events.json").read_text(encoding="utf-8") == committed   # rebuilt = same
    wf = json.loads(committed)
    names = {n["name"] for n in wf["nodes"]}
    for src, conn in wf["connections"].items():
        assert src in names
        assert all(t["node"] in names for out in conn["main"] for t in out)
    hook = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook")
    assert hook["parameters"]["authentication"] == "headerAuth" and hook["parameters"]["path"] == "clinic-events"
    assert all(n["parameters"].get("emailType") == "text" for n in wf["nodes"] if n["type"] == "n8n-nodes-base.gmail")
    headers = (ROOT / "automation" / "bookings_sheet_headers.csv").read_text().strip().split(",")
    row = next(n for n in wf["nodes"] if n["name"] == "Booking row")
    assert [a["name"] for a in row["parameters"]["assignments"]["assignments"]] == headers
