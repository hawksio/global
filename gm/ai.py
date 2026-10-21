"""AI Email Assistant: drafting, subject lines, tone rewrites, proofreading, follow-ups, variations, campaign
summaries and recommendations, plus a content checker.

Providers (GM_AI_PROVIDER):
  anthropic - Claude through the official Anthropic SDK (needs an API key; usage is metered and capped).
  rules     - free, local and deterministic. Fills structured templates from what you typed and does mechanical
              clean-ups. It is clearly weaker than a language model, and says so in the UI.
  mock      - canned output for the sandbox and tests, labelled as such.
  auto      - anthropic when a key is configured, otherwise rules.

Guard rails: the model is told never to invent facts about the recipient or their company, relationships,
testimonials, statistics or financial promises, and to leave [square-bracket placeholders] where information is
missing. Generated content is saved unapproved and campaigns built from it need human approval.
"""
import re
from dataclasses import dataclass, field

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from .db import execute, now, query, scalar
from .security import audit, current_user, get_secret, require, throttle, workspace, ws_id

bp = Blueprint("ai", __name__, url_prefix="/assistant")

TONES = {"professional": "Professional", "friendly": "Friendly", "concise": "Concise", "formal": "Formal"}
PRICE_PER_MTOK = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}

SYSTEM = """You help a business write honest, professional B2B email. Rules you must follow:
- Use only facts the user gave you. Never invent details about the recipient, their company, a prior relationship or
  conversation, customer names, testimonials, statistics, awards, prices, guarantees or financial promises.
- Where a useful detail is missing, write a short placeholder in square brackets, e.g. [one concrete benefit].
- No deceptive subject lines (no fake "Re:" or "Fwd:", no false urgency, no clickbait), no spammy wording, no ALL CAPS.
- Keep the sender's identity clear. Do not impersonate anyone.
- You may use these personalisation fields exactly as written: {{first_name|there}}, {{company}}, {{sender_name}},
  {{sender_company}}. Do not add an unsubscribe line; the system adds one.
- Output plain text only, in exactly the format requested."""


@dataclass
class AIResult:
    text: str
    provider: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    ok: bool = True
    error: str = ""
    notes: list = field(default_factory=list)


class AIError(Exception):
    pass


# ---------------------------------------------------------------- providers
class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key, model, effort):
        self.api_key, self.model, self.effort = api_key, model, effort

    def complete(self, prompt, max_tokens=4000):
        import anthropic
        client = anthropic.Anthropic(api_key=self.api_key, timeout=60.0, max_retries=2)
        try:
            resp = client.beta.messages.create(
                model=self.model, max_tokens=max_tokens, system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.effort},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
        except anthropic.AuthenticationError:
            raise AIError("The AI provider rejected the API key. Check it under Integrations.")
        except anthropic.PermissionDeniedError:
            raise AIError("The AI API key is not allowed to use this model.")
        except anthropic.RateLimitError:
            raise AIError("The AI provider is rate-limiting requests. Try again in a minute.")
        except anthropic.BadRequestError as exc:
            raise AIError(f"The AI provider could not process the request: {exc.message[:200]}")
        except anthropic.APIStatusError as exc:
            raise AIError(f"The AI provider returned an error ({exc.status_code}). Try again later.")
        except anthropic.APIConnectionError:
            raise AIError("Could not reach the AI provider. Check the server's internet connection.")
        if resp.stop_reason == "refusal":
            raise AIError("The AI declined this request. Rephrase it or write the email manually.")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            raise AIError("The AI returned an empty answer. Try again.")
        notes = ["The answer was cut short; consider asking for something shorter."] if resp.stop_reason == "max_tokens" else []
        return AIResult(text=text, provider=self.name, model=resp.model or self.model,
                        input_tokens=resp.usage.input_tokens or 0, output_tokens=resp.usage.output_tokens or 0, notes=notes)


class MockProvider:
    name = "mock"

    def complete(self, prompt, max_tokens=0):
        if "SUBJECT LINES" in prompt:
            text = "[Mock AI] A quick idea for {{company}}\n[Mock AI] Question about your [area]\n[Mock AI] Could this help {{company}}?"
        elif "SUMMARY" in prompt:
            text = "[Mock AI] This is a sandbox summary. Connect an AI provider for real analysis."
        else:
            text = ("SUBJECT: [Mock AI] A quick idea for {{company}}\n---\nHi {{first_name|there}},\n\n[Mock AI] This is "
                    "sample text from the sandbox assistant. [one concrete benefit]\n\nBest regards,\n{{sender_name}}")
        return AIResult(text=text, provider=self.name, model="mock", notes=["Sandbox mock output (not written by an AI model)."])


def provider():
    choice = current_app.config["AI_PROVIDER"]
    if current_app.config.get("AI_PROVIDER_OVERRIDE"):
        return current_app.config["AI_PROVIDER_OVERRIDE"]
    key = current_app.config["ANTHROPIC_API_KEY"] or get_secret(0, "anthropic_api_key")
    if choice == "mock":
        return MockProvider()
    if choice in ("anthropic", "auto") and key:
        return AnthropicProvider(key, current_app.config["AI_MODEL"], current_app.config["AI_EFFORT"])
    return None  # rule-based


def monthly_usage(workspace_id):
    return scalar("SELECT COUNT(*) FROM ai_usage WHERE workspace_id = ? AND provider = 'anthropic' AND ok = 1 "
                  "AND created_at >= strftime('%Y-%m-01 00:00:00', 'now')", (workspace_id,)) or 0


def _record(task, result):
    price = PRICE_PER_MTOK.get(result.model, PRICE_PER_MTOK.get(current_app.config["AI_MODEL"], (0, 0)))
    cost = int(result.input_tokens * price[0] + result.output_tokens * price[1])  # micro-dollars
    user = current_user()
    execute("INSERT INTO ai_usage (workspace_id, user_id, task, provider, model, input_tokens, output_tokens, cost_micro_usd, ok, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user["workspace_id"], user["id"], task, result.provider, result.model, result.input_tokens,
             result.output_tokens, cost, int(result.ok), now()))


def run(task, prompt, fallback):
    """Run a prompt on the configured provider, or the rule-based fallback. Always returns an AIResult."""
    p = provider()
    ws = workspace()
    if p is not None and p.name == "anthropic" and monthly_usage(ws["id"]) >= ws["ai_monthly_limit"]:
        res = fallback()
        res.notes.append(f"Monthly AI limit ({ws['ai_monthly_limit']} requests) reached, so the free rule-based assistant "
                         "was used instead. An administrator can raise the limit in Settings.")
    elif p is None:
        res = fallback()
    else:
        try:
            res = p.complete(prompt)
        except AIError as exc:
            _record(task, AIResult(text="", provider=p.name, model=getattr(p, "model", ""), ok=False))
            res = fallback()
            res.notes.append(f"{exc} The rule-based assistant was used instead.")
            res.error = str(exc)
            return res
    _record(task, res)
    return res


# ---------------------------------------------------------------- rule-based assistant
FILLERS = [r"\bI (just )?wanted to reach out( to you)?( to)?\b", r"\bI hope this (email|message) finds you well[.,]?\s*",
           r"\bjust\b", r"\breally\b", r"\bvery\b", r"\bbasically\b", r"\bactually\b", r"\bin order to\b"]
CONTRACTIONS = {"don't": "do not", "can't": "cannot", "won't": "will not", "I'm": "I am", "we're": "we are",
                "it's": "it is", "you're": "you are", "isn't": "is not", "doesn't": "does not", "I'd": "I would",
                "we'd": "we would", "let's": "let us", "that's": "that is", "there's": "there is", "we've": "we have",
                "I've": "I have", "aren't": "are not", "couldn't": "could not", "wouldn't": "would not"}
MISSPELLINGS = {"recieve": "receive", "seperate": "separate", "occured": "occurred", "definately": "definitely",
                "accomodate": "accommodate", "untill": "until", "wich": "which", "teh": "the", "thier": "their",
                "acheive": "achieve", "adress": "address", "begining": "beginning", "buisness": "business",
                "calender": "calendar", "commited": "committed", "concious": "conscious", "existance": "existence",
                "goverment": "government", "independant": "independent", "occassion": "occasion",
                "publically": "publicly", "reccomend": "recommend", "refered": "referred", "succesful": "successful",
                "tommorow": "tomorrow", "truely": "truly", "wierd": "weird", "alot": "a lot", "noone": "no one"}


def proofread_text(text):
    changes = []
    out = text or ""
    for wrong, right in MISSPELLINGS.items():
        pattern = re.compile(rf"\b{wrong}\b", re.I)
        if pattern.search(out):
            changes.append(f"“{wrong}” → “{right}”")
            out = pattern.sub(right, out)
    new = re.sub(r"\b(\w+)\s+\1\b", r"\1", out, flags=re.I)
    if new != out:
        changes.append("Removed a repeated word")
        out = new
    new = re.sub(r"[ \t]{2,}", " ", out)
    new = re.sub(r"\s+([,.;:!?])", r"\1", new)
    new = re.sub(r"([,;:])(?=[A-Za-z])", r"\1 ", new)
    new = re.sub(r"(?<![\w.])i(?![\w.])", "I", new)
    new = re.sub(r"(^|[.!?]\s+|\n)([a-z])", lambda m: m.group(1) + m.group(2).upper(), new)
    new = re.sub(r"!{2,}", "!", new)
    if new != out:
        changes.append("Fixed spacing, capitalisation and punctuation")
        out = new
    return out.strip(), changes


def rewrite_text(text, tone):
    out = text or ""
    if tone in ("concise", "professional"):
        for f in FILLERS:
            out = re.sub(f, "", out, flags=re.I)
        out = re.sub(r"[ \t]{2,}", " ", out)
    if tone == "formal":
        for short, full in CONTRACTIONS.items():
            out = re.sub(rf"\b{re.escape(short)}\b", full, out)
            out = re.sub(rf"\b{re.escape(short.capitalize())}\b", full.capitalize(), out)
        out = re.sub(r"^(Hi|Hey)\b", "Dear", out.strip(), flags=re.M)
        out = re.sub(r"\b(Cheers|Thanks)\b,?\s*$", "Kind regards,", out, flags=re.M)
    if tone == "friendly":
        out = re.sub(r"^Dear\b", "Hi", out.strip(), flags=re.M)
        out = re.sub(r"\b(Kind|Best|Warm) regards,?", "Thanks,", out)
    if tone == "concise":
        paragraphs = [p.strip() for p in out.split("\n\n") if p.strip()]
        out = "\n\n".join(p if len(p) < 400 else " ".join(re.split(r"(?<=[.!?])\s+", p)[:3]) for p in paragraphs)
    return proofread_text(out)[0]


def rules_draft(i):
    offer = i.get("offer") or "[what you offer]"
    audience = i.get("audience") or "[the kind of business you are writing to]"
    goal = i.get("goal") or "a short call"
    benefit = i.get("benefit") or "[one concrete, accurate benefit]"
    greeting = "Dear {{first_name|there}}," if i.get("tone") == "formal" else "Hi {{first_name|there}},"
    body = (f"{greeting}\n\nI'm {{{{sender_name}}}} from {{{{sender_company}}}}. We help {audience} with {offer}.\n\n"
            f"{benefit}\n\nWould you be open to {goal}? If this isn't relevant to {{{{company}}}}, just let me know and "
            f"I won't follow up.\n\n{'Kind regards' if i.get('tone') == 'formal' else 'Best regards'},\n{{{{sender_name}}}}")
    subject = rules_subjects(i)[0]
    return f"SUBJECT: {subject}\n---\n{rewrite_text(body, i.get('tone', 'professional'))}"


def rules_subjects(i):
    offer = (i.get("offer") or "your team").strip().rstrip(".")
    short = offer if len(offer) < 40 else offer[:37].rsplit(" ", 1)[0]
    return [f"{short[:1].upper()}{short[1:]} for {{{{company}}}}", f"Question about {short}",
            f"An idea for {{{{company}}}}", f"Could {short} help {{{{company}}}}?", f"{{{{first_name|Hello}}}}, a quick question"]


def rules_followup(i):
    return ("SUBJECT: Following up: {original}\n---\nHi {{{{first_name|there}}}},\n\nI'm following up on my note about "
            "{offer}. I know inboxes get busy, so I'll keep this short: {benefit}\n\nIs this worth a conversation, or "
            "should I close the loop? Either answer is fine.\n\nBest regards,\n{{{{sender_name}}}}").format(
        original=i.get("subject") or "my last email", offer=i.get("offer") or "[your offer]",
        benefit=i.get("benefit") or "[one concrete, accurate benefit]")


def _local(text, notes=None):
    return AIResult(text=text, provider="rules", model="rules", notes=notes or [
        "Written by the free rule-based assistant from what you entered. Review and edit it before use."])


# ---------------------------------------------------------------- content checks (always local)
SPAM_PHRASES = ["100% free", "act now", "risk-free", "risk free", "guaranteed", "no obligation", "winner", "you have been selected",
                "cash bonus", "earn money", "make money", "double your", "click here", "limited time only", "urgent",
                "once in a lifetime", "no catch", "100% satisfied", "miracle", "lowest price", "best price", "dear friend",
                "congratulations", "buy now", "order now", "this isn't spam", "not spam", "free money", "credit card"]
CLAIM_PHRASES = [r"\bguarantee[sd]?\b", r"\bproven\b", r"\b#1\b", r"\bnumber one\b", r"\bbest in the (world|industry|market)\b",
                 r"\bleading\b", r"\bworld[- ]class\b", r"\b\d{2,3}% (more|less|increase|growth|better|faster|cheaper)\b",
                 r"\bdouble (your|the)\b", r"\btriple\b", r"\b(clients?|customers?) (say|love)\b", r"\btestimonial\b",
                 r"\bno risk\b", r"\bguaranteed (results|returns|roi)\b", r"\breturns? of\b"]
SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly", "rebrand.ly", "cutt.ly", "shorturl.at",
              "rb.gy", "tiny.cc"}
RISKY_TLDS = {"zip", "mov", "top", "xyz", "click", "loan", "work", "gq", "tk", "ml", "cf", "ga"}


def content_check(subject, html_body, text_body=""):
    """Rule-based review. Returns a list of {level, title, detail}."""
    from urllib.parse import urlsplit
    findings = []
    body_text = (text_body or "") + " " + re.sub(r"<[^>]+>", " ", html_body or "")
    combined = f"{subject} {body_text}".lower()

    def add(level, title, detail):
        findings.append({"level": level, "title": title, "detail": detail})

    if not subject.strip():
        add("error", "Missing subject", "Every email needs a subject line.")
    if re.match(r"^\s*(re|fwd?|fw)\s*:", subject, re.I):
        add("error", "Misleading subject prefix", "Don't start with Re: or Fwd: unless it really is a reply or forward.")
    if subject and sum(1 for ch in subject if ch.isupper()) > max(8, len(subject) * 0.5):
        add("warn", "Subject is mostly capitals", "All-caps subjects look like shouting and are often filtered.")
    if subject.count("!") > 1 or combined.count("!!"):
        add("warn", "Too many exclamation marks", "Use at most one, or none.")
    if len(subject) > 90:
        add("info", "Long subject line", "Many inboxes cut subjects off after about 60 characters.")
    for phrase in SPAM_PHRASES:
        if phrase in combined:
            add("warn", f"Spam-like wording: “{phrase}”", "Rephrase plainly; this wording is common in unwanted email.")
    for pattern in CLAIM_PHRASES:
        m = re.search(pattern, combined)
        if m:
            add("warn", f"Claim needs evidence: “{m.group(0)}”",
                "Only keep claims you can substantiate. Never invent results, testimonials or guarantees.")
    if re.search(r"\[[^\]]{3,80}\]", body_text):
        add("error", "Unfilled placeholder", "Replace every [square-bracket placeholder] with real information or remove it.")
    for m in re.finditer(r"<a\s[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html_body or "", re.I | re.S):
        href, label = m.group(1).strip(), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if href.startswith(("{{", "mailto:", "tel:", "#")):
            continue
        parts = urlsplit(href)
        host = (parts.hostname or "").lower()
        if parts.scheme == "http":
            add("warn", "Insecure link", f"{href} uses http://. Use https:// where possible.")
        if parts.scheme not in ("http", "https"):
            add("error", "Unusual link", f"{href} is not a web link.")
        if host in SHORTENERS:
            add("warn", "Link shortener", f"{host} hides the destination; spam filters and readers distrust it.")
        if re.fullmatch(r"[\d.]+", host or "x"):
            add("error", "Link to a raw IP address", f"{href} looks suspicious; link to a named domain.")
        if host.rsplit(".", 1)[-1] in RISKY_TLDS:
            add("warn", "Unusual link domain", f"{host} uses a domain ending often associated with abuse.")
        shown = re.search(r"([a-z0-9-]+\.)+[a-z]{2,}", label.lower())
        if shown and host and not (host.endswith(shown.group(0)) or shown.group(0).endswith(host)):
            add("error", "Link text doesn't match destination",
                f"The text shows {shown.group(0)} but the link goes to {host}. This looks like phishing.")
    words = len(body_text.split())
    links = len(re.findall(r"<a\s", html_body or "", re.I))
    if words and links > max(3, words // 40):
        add("warn", "Many links", "A high link-to-text ratio looks promotional. Keep links few and relevant.")
    if re.search(r"<img\b", html_body or "", re.I) and words < 30:
        add("warn", "Mostly images", "Image-heavy emails with little text are often filtered and inaccessible.")
    if not findings:
        add("ok", "No problems found by the automatic checks", "A person should still read it before sending.")
    return findings


# ---------------------------------------------------------------- views
def parse_draft(text):
    if "---" in text and text.strip().upper().startswith("SUBJECT:"):
        head, body = text.split("---", 1)
        return head.split(":", 1)[1].strip(), body.strip()
    m = re.match(r"\s*subject:\s*(.+)\n", text, re.I)
    if m:
        return m.group(1).strip(), text[m.end():].strip()
    return "", text.strip()


def campaign_metrics(campaign_id, workspace_id):
    from .analytics import campaign_stats
    return campaign_stats(campaign_id, workspace_id)


@bp.route("/", methods=["GET", "POST"])
@require("use_ai")
def index():
    wid = ws_id()
    i = {k: request.form.get(k, "").strip()[:4000] for k in
         ("task", "offer", "audience", "goal", "benefit", "tone", "text", "subject", "campaign_id", "facts")}
    i["tone"] = i["tone"] if i["tone"] in TONES else "professional"
    result, draft, subjects, findings = None, None, None, None
    if request.method == "POST":
        if throttle(f"ai:{current_user()['id']}", 30, 600, "You are using the assistant very quickly. Wait a few minutes."):
            return redirect(url_for("ai.index"))
        task = i["task"]
        facts = f"\nAdditional facts supplied by the user (true, may be used): {i['facts']}" if i["facts"] else ""
        if task == "draft":
            prompt = (f"Write a first-contact email. Offer: {i['offer']}\nAudience: {i['audience']}\nGoal / call to action: "
                      f"{i['goal']}\nKey benefit: {i['benefit']}\nTone: {i['tone']}{facts}\n\nFormat: a line 'SUBJECT: ...', a "
                      "line '---', then the body. Under 150 words.")
            result = run(task, prompt, lambda: _local(rules_draft(i)))
            draft = parse_draft(result.text)
        elif task == "subjects":
            prompt = (f"Write 5 honest SUBJECT LINES (one per line, no numbering, under 60 characters) for an email about: "
                      f"{i['offer'] or i['text']}\nAudience: {i['audience']}{facts}")
            result = run(task, prompt, lambda: _local("\n".join(rules_subjects(i))))
            subjects = [s.strip(" -•\t") for s in result.text.splitlines() if s.strip()][:8]
        elif task == "rewrite":
            prompt = (f"Rewrite this email in a {i['tone']} tone. Keep every fact; add none.\nFormat: the rewritten text "
                      f"only.\n\n{i['text']}")
            result = run(task, prompt, lambda: _local(rewrite_text(i["text"], i["tone"])))
        elif task == "proofread":
            prompt = ("Correct spelling, grammar and clarity in this email without changing its meaning or adding facts. "
                      f"Format: the corrected text only.\n\n{i['text']}")
            fixed, changes = proofread_text(i["text"])
            result = run(task, prompt, lambda: _local(fixed, ["Rule-based fixes: " + ("; ".join(changes) or "none needed")]))
        elif task == "followup":
            prompt = (f"Write a short, polite follow-up to an earlier email with subject '{i['subject']}' about {i['offer']}. "
                      f"Key benefit: {i['benefit']}{facts}. Offer an easy way to say no. Format: 'SUBJECT: ...', '---', "
                      "body. Under 100 words.")
            result = run(task, prompt, lambda: _local(rules_followup(i)))
            draft = parse_draft(result.text)
        elif task == "variations":
            prompt = (f"Write 2 alternative versions of this email for an A/B comparison. Keep all facts, add none. Separate "
                      f"them with a line '==='.\n\n{i['text']}")
            result = run(task, prompt, lambda: _local(rewrite_text(i["text"], "concise") + "\n===\n" +
                                                       rewrite_text(i["text"], "friendly")))
        elif task in ("summarize", "recommend"):
            cid = int(i["campaign_id"]) if i["campaign_id"].isdigit() else 0
            if not query("SELECT 1 FROM campaigns WHERE id = ? AND workspace_id = ?", (cid, wid), one=True):
                flash("Choose a campaign.", "error")
                return redirect(url_for("ai.index"))
            stats = campaign_metrics(cid, wid)
            from .analytics import rule_recommendations, rule_summary
            prompt = (f"CAMPAIGN SUMMARY task. These are the only data you have (real numbers from the app; 'n/a' means "
                      f"unavailable). Opens are approximate. Write a short {'summary' if task == 'summarize' else 'list of 3-5 practical recommendations'} "
                      f"based only on these numbers; say when the sample is too small to conclude anything.\n\n{stats}")
            fallback = rule_summary(stats) if task == "summarize" else "\n".join(rule_recommendations(stats))
            result = run(task, prompt, lambda: _local(fallback, ["Generated by rules from your real campaign numbers."]))
        elif task == "check":
            findings = content_check(i["subject"], i["text"].replace("\n", "<br>"), i["text"])
        else:
            abort(400)
    campaigns = query("SELECT id, name FROM campaigns WHERE workspace_id = ? ORDER BY id DESC LIMIT 50", (wid,))
    p = provider()
    return render_template("ai/index.html", i=i, result=result, draft=draft, subjects=subjects, findings=findings,
                           campaigns=campaigns, TONES=TONES, provider_name=p.name if p else "rules",
                           usage=monthly_usage(wid), limit=workspace()["ai_monthly_limit"],
                           draft_findings=content_check(draft[0], draft[1].replace("\n", "<br>"), draft[1]) if draft else None)


@bp.route("/save", methods=["POST"])
@require("edit_templates")
def save_as_template():
    subject = request.form.get("subject", "").strip()[:300]
    body = request.form.get("body", "").strip()[:50000]
    if not body:
        flash("Nothing to save.", "error")
        return redirect(url_for("ai.index"))
    from .render import unknown_fields
    bad = unknown_fields(subject, body)
    if bad:
        flash("Unknown personalisation fields: " + ", ".join(bad), "error")
        return redirect(url_for("ai.index"))
    from .templates_lib import create_template, text_to_html
    tid = create_template(ws_id(), request.form.get("name", "").strip()[:120] or f"AI draft {now()[:16]}", "AI drafts",
                          subject, text_to_html(body), body, ai_generated=True)
    audit("template.created_from_ai", target=tid)
    flash("Saved as a template. It is marked AI-generated and must be reviewed and approved before use in a campaign.", "success")
    return redirect(url_for("templates.edit", template_id=tid))
