"""Sandbox sample data: fictional contacts (reserved example.* domains), templates and a simulation campaign.

Everything created here is marked sandbox = 1 or named "[Sample]" and is excluded from campaign segments by default.
"""
import json

from .contacts import apply_tags, create_contact
from .db import execute, now, query

PEOPLE = [
    ("Ada", "Fictional", "Northwind Sample Traders", "Operations Manager", "United Kingdom", "Logistics"),
    ("Ben", "Placeholder", "Contoso Demo Ltd", "Director", "Ireland", "Software"),
    ("Chloe", "Testperson", "Fabrikam Example GmbH", "Head of Marketing", "Germany", "Manufacturing"),
    ("Dev", "Sample", "Tailspin Example Co", "Founder", "United States", "Travel"),
    ("Elena", "Mock", "Wingtip Sample SA", "Procurement Lead", "Spain", "Retail"),
    ("Femi", "Demo", "Litware Example Inc", "CTO", "Nigeria", "Software"),
    ("Grace", "Imaginary", "Adventure Works Sample", "Sales Director", "Australia", "Outdoor"),
    ("Hiro", "Example", "Proseware Demo KK", "Marketing Lead", "Japan", "Media"),
]
DOMAINS = ["example.com", "example.net", "example.org"]
SAMPLE_DETAIL = json.dumps({"summary": "SANDBOX SAMPLE: fictional address on a reserved example domain; no real checks were run",
                            "checks": []})


def seed(workspace_id, user_id=None):
    if query("SELECT 1 FROM contacts WHERE workspace_id = ? AND sandbox = 1", (workspace_id,), one=True):
        return False
    ids = []
    for i, (first, last, company, title, country, industry) in enumerate(PEOPLE):
        email = f"{first.lower()}.{last.lower()}@{DOMAINS[i % 3]}"
        cid, _ = create_contact(workspace_id, email, {"first_name": first, "last_name": last, "company": company,
                                                      "job_title": title, "country": country, "industry": industry},
                                "sandbox", source_detail="Sandbox sample data (fictional)", purpose="Trying out the app",
                                recipient_type="business", lawful_basis="legitimate_interest", sandbox=1)
        execute("UPDATE contacts SET verification_status = 'validated', verification_checked_at = ?, verification_detail = ? "
                "WHERE id = ?", (now(), SAMPLE_DETAIL, cid))
        apply_tags(workspace_id, cid, ["sample"])
        ids.append(cid)
    body = ("<p>Hi {{first_name|there}},</p><p>I'm {{sender_name}} from {{sender_company}}. We help teams like "
            "{{company}} plan their outreach more carefully.</p><p>Would a 15-minute call next week be useful? If it isn't "
            "relevant, just let me know.</p><p>Best regards,<br>{{sender_name}}</p>")
    tid = execute("INSERT INTO templates (workspace_id, name, category, subject, html_body, text_body, created_by, created_at, "
                  "updated_at) VALUES (?, '[Sample] First contact', 'First contact', 'A quick idea for {{company}}', ?, '', ?, ?, ?)",
                  (workspace_id, body, user_id, now(), now()))
    execute("INSERT INTO template_versions (template_id, version, subject, html_body, text_body, created_by, created_at) "
            "VALUES (?, 1, 'A quick idea for {{company}}', ?, '', ?, ?)", (tid, body, user_id, now()))
    segment = json.dumps({"tag": "sample", "verification_status": ["validated"], "status": ["active"]})
    ws = query("SELECT * FROM workspaces WHERE id = ?", (workspace_id,), one=True)
    cid = execute("INSERT INTO campaigns (workspace_id, name, template_id, segment, from_name, from_email, timezone, dry_run, "
                  "send_days, created_by, created_at, updated_at) VALUES (?, '[Sample] Simulation campaign', ?, ?, ?, ?, ?, 1, "
                  "'0,1,2,3,4,5,6', ?, ?, ?)", (workspace_id, tid, segment, ws["sender_name"] or ws["name"],
                                                 ws["sender_email"] or "sender@example.com", ws["timezone"], user_id, now(), now()))
    execute("INSERT INTO campaign_steps (campaign_id, step_no, delay_days, subject, html_body, text_body) VALUES (?, 0, 0, ?, ?, '')",
            (cid, "A quick idea for {{company}}", body))
    execute("INSERT INTO campaign_steps (campaign_id, step_no, delay_days, subject, html_body, text_body) VALUES (?, 1, 3, ?, ?, '')",
            (cid, "Following up: a quick idea for {{company}}",
             "<p>Hi {{first_name|there}},</p><p>Just following up on my last note. Is this worth a short conversation?</p>"
             "<p>Best regards,<br>{{sender_name}}</p>"))
    return True
