"""Writes n8n_clinic_events.json (the workflow to import into n8n). Kept as code so changes are reviewable:
    python automation/build_workflow.py
"""
import json
import uuid
from pathlib import Path

NS = uuid.UUID("6f1c2a52-6a39-4a8e-9a3c-2d1f6d1c0e11")
def uid(name): return str(uuid.uuid5(NS, name))            # stable ids, so the file doesn't change on every build
EVENT = "$('Clinic events').item.json.body"     # read from the webhook itself, not from earlier steps
D = EVENT + ".data"
SETTING = "$('Settings').item.json"

def setnode(name, pos, fields, keep_others=False):
    return {"id": uid(name), "name": name, "type": "n8n-nodes-base.set", "typeVersion": 3.4, "position": pos,
            "parameters": {"assignments": {"assignments": [
                {"id": uid(name + k), "name": k, "value": v, "type": "string"} for k, v in fields.items()]},
                "includeOtherFields": keep_others, "options": {}}}

def gmail(name, pos):
    return {"id": uid(name), "name": name, "type": "n8n-nodes-base.gmail", "typeVersion": 2.1, "position": pos,
            "webhookId": uid(name + "hook"),
            "parameters": {"sendTo": "={{ $json.to }}", "subject": "={{ $json.subject }}", "emailType": "text",
                           "message": "={{ $json.text }}", "options": {"appendAttribution": False}}}

def rule(output, event):
    return {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                           "conditions": [{"id": uid(output), "leftValue": "={{ " + EVENT + ".event }}", "rightValue": event,
                                           "operator": {"type": "string", "operation": "equals"}}],
                           "combinator": "and"},
            "renameOutput": True, "outputKey": output}

nodes = [
    {"id": uid("webhook"), "name": "Clinic events", "type": "n8n-nodes-base.webhook", "typeVersion": 2.1,
     "position": [0, 300], "webhookId": uid("webhook-path"),
     # Reply only when the workflow has finished. On Cloud Run (request-based billing) the server only gets CPU
     # while answering a request, so the email and sheet row must be done before the reply, not after.
     "parameters": {"httpMethod": "POST", "path": "clinic-events", "authentication": "headerAuth",
                    "responseMode": "lastNode", "options": {}}},
    setnode("Settings", [220, 300], {
        "staff_email": "frontdesk@example.com",                                   # <- change to your email
        "sheet_url": "https://docs.google.com/spreadsheets/d/PASTE-YOUR-SHEET-ID/edit",   # <- your Google Sheet
    }),
    {"id": uid("switch"), "name": "Which event?", "type": "n8n-nodes-base.switch", "typeVersion": 3.2,
     "position": [440, 300],
     "parameters": {"rules": {"values": [rule("Booked", "appointment.booked"), rule("Handoff", "handoff.created")]},
                    "options": {}}},
    setnode("Booking row", [700, 120], {
        "Booked at": "={{ $now.setZone('America/New_York').toFormat('yyyy-MM-dd HH:mm') }}",
        "Appointment": f"={{{{ {D}.when }}}}",
        "Visit": f"={{{{ {D}.visit_label }}}}",
        "Therapist": f"={{{{ {D}.provider }}}}",
        "Patient": f"={{{{ {D}.name }}}}",
        "Phone": f"={{{{ {D}.phone }}}}",
        "Email": f"={{{{ {D}.email || '' }}}}",
        "Reason": f"={{{{ {D}.reason || '' }}}}",
        "Appointment ID": f"={{{{ String({D}.appointment_id) }}}}",
    }),
    {"id": uid("sheet"), "name": "Add to Bookings sheet", "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.5,
     "position": [940, 120],
     "onError": "continueRegularOutput",        # a sheet problem must never stop the staff email
     "parameters": {"operation": "append",
                    "documentId": {"__rl": True, "mode": "url", "value": "={{ $('Settings').item.json.sheet_url }}"},
                    "sheetName": {"__rl": True, "mode": "name", "value": "Bookings"},
                    "columns": {"mappingMode": "autoMapInputData", "value": {}, "matchingColumns": [], "schema": []},
                    "options": {}}},
    setnode("Booking email", [700, 300], {
        "to": "={{ " + SETTING + ".staff_email }}",
        "subject": f"=New booking: {{{{ {D}.name }}}}, {{{{ {D}.when }}}}",
        "text": ("=A new appointment was booked through the website assistant.\n\n"
                 f"Patient:  {{{{ {D}.name }}}}\n"
                 f"Phone:    {{{{ {D}.phone }}}}\n"
                 f"Email:    {{{{ {D}.email || 'not given' }}}}\n"
                 f"Visit:    {{{{ {D}.visit_label }}}} with {{{{ {D}.provider }}}}\n"
                 f"When:     {{{{ {D}.when }}}}\n"
                 f"Reason:   {{{{ {D}.reason || 'not given' }}}}\n\n"
                 f"Appointment #{{{{ {D}.appointment_id }}}}. It's also in the Bookings sheet.\n"
                 "Please verify insurance before the visit."),
    }),
    gmail("Email the front desk", [940, 300]),
    setnode("Handoff email", [700, 500], {
        "to": "={{ " + SETTING + ".staff_email }}",
        "subject": f"=Patient asked for a person (request #{{{{ {D}.handoff_id }}}})",
        "text": ("=Someone on the website asked to talk to a person.\n\n"
                 f"Name:     {{{{ {D}.name || 'not given' }}}}\n"
                 f"Phone:    {{{{ {D}.phone || 'not given: check the conversation' }}}}\n"
                 f"Email:    {{{{ {D}.email || 'not given' }}}}\n"
                 f"Reason:   {{{{ {D}.reason }}}}\n\n"
                 f"Summary:\n{{{{ {D}.summary }}}}\n\n"
                 "Please call them back today."),
    }),
    gmail("Email the front desk (handoff)", [940, 500]),
]

def link(*targets): return [[{"node": t, "type": "main", "index": 0} for t in targets]]
connections = {
    "Clinic events": {"main": link("Settings")},
    "Settings": {"main": link("Which event?")},
    "Which event?": {"main": [[{"node": "Booking row", "type": "main", "index": 0},
                               {"node": "Booking email", "type": "main", "index": 0}],
                              [{"node": "Handoff email", "type": "main", "index": 0}]]},
    "Booking row": {"main": link("Add to Bookings sheet")},
    "Booking email": {"main": link("Email the front desk")},
    "Handoff email": {"main": link("Email the front desk (handoff)")},
}
workflow = {"name": "Clinic Concierge: bookings and handoffs", "nodes": nodes, "connections": connections,
            "settings": {"executionOrder": "v1", "timezone": "America/New_York"}, "pinData": {}, "active": False,
            "meta": {"templateCredsSetupCompleted": False}}
out = Path(__file__).with_name("n8n_clinic_events.json")
out.write_text(json.dumps(workflow, indent=2) + "\n", encoding="utf-8")
print(f"wrote {out}")
