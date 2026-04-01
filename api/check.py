import csv
import json
import os
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler
from io import StringIO
from urllib.parse import parse_qs, quote, urlparse

import requests
from bs4 import BeautifulSoup


# Fixed monitor settings for this one-page watcher.
MONITOR_URL = "https://www.wgai.co.in/pages/membership-information.php"
TARGET_SUBSTRING = "Entry Form for Amateur Players"
MAX_MATCH_LENGTH = 100
KV_KEY = "wgai:entry_form_for_amateur_players"
REQUEST_TIMEOUT_SECONDS = 20
HARDCODED_INITIAL_TEXT = "Entry Form for Amateur Players - Season 2026 (Leg 5 to 6)"
BASELINE_CAPTURED_AT = "March 31, 2026"
MONITOR_STARTED_AT = "April 1, 2026"
HISTORY_CSV_URL = (
    "https://raw.githubusercontent.com/"
    "nikhilsahni10/wgai-entry-form-tracker/main/data/check_history.csv"
)
MAX_HISTORY_ROWS = 200
STALE_AFTER_MINUTES = 12
IST = timezone(timedelta(hours=5, minutes=30))


# Read the required environment variables once per invocation.
def load_config():
    config = {
        "telegram_token": os.environ.get("TELEGRAM_TOKEN", "").strip(),
        "chat_id": os.environ.get("CHAT_ID", "").strip(),
        "kv_rest_api_url": os.environ.get("KV_REST_API_URL", "").strip(),
        "kv_rest_api_token": os.environ.get("KV_REST_API_TOKEN", "").strip(),
    }
    return config


# Normalize whitespace so the stored value is stable across minor HTML spacing changes.
def normalize_text(value):
    return " ".join(value.split())


# Find the shortest matching element text so we avoid parent containers like
# "Entry Form... Click to submit" and store only the text being monitored.
def extract_target_text(html):
    soup = BeautifulSoup(html, "html.parser")
    matches = []

    for tag in soup.find_all(True):
        text = normalize_text(tag.get_text(" ", strip=True))
        if TARGET_SUBSTRING in text and len(text) < MAX_MATCH_LENGTH:
            matches.append(text)

    if not matches:
        raise ValueError(
            f'No element text containing "{TARGET_SUBSTRING}" under '
            f"{MAX_MATCH_LENGTH} characters was found."
        )

    return min(matches, key=lambda item: (len(item), item))


# Fetch the live page and extract the current watched text.
def fetch_current_text():
    response = requests.get(
        MONITOR_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; WGAITextMonitor/1.0)"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return extract_target_text(response.text)


# Minimal REST helpers for Vercel KV / Upstash Redis using the provided env vars.
def kv_headers(token):
    return {"Authorization": f"Bearer {token}"}


def kv_get(rest_url, token, key):
    response = requests.post(
        f"{rest_url.rstrip('/')}/get/{quote(key, safe='')}",
        headers=kv_headers(token),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("result")


def kv_set(rest_url, token, key, value):
    response = requests.post(
        f"{rest_url.rstrip('/')}/set/{quote(key, safe='')}",
        headers={
            **kv_headers(token),
            "Content-Type": "text/plain; charset=utf-8",
        },
        data=value.encode("utf-8"),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("result") != "OK":
        raise RuntimeError(f"KV set failed: {payload}")


# Telegram is used for first-run confirmation, change alerts, and failure alerts.
def send_telegram_message(token, chat_id, message):
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": message},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")


def detect_chat_id(token):
    response = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram getUpdates failed: {payload}")

    for update in reversed(payload.get("result", [])):
        message = update.get("message") or update.get("channel_post")
        if message and message.get("chat", {}).get("id") is not None:
            return str(message["chat"]["id"])

        callback_query = update.get("callback_query")
        if callback_query:
            chat = callback_query.get("message", {}).get("chat", {})
            if chat.get("id") is not None:
                return str(chat["id"])

    return ""


def send_failure_alert(config, reason):
    if not config.get("telegram_token") or not config.get("chat_id"):
        return

    message = (
        "WGAI monitoring failed.\n\n"
        f"Reason:\n{reason}\n\n"
        f"URL:\n{MONITOR_URL}"
    )

    try:
        send_telegram_message(config["telegram_token"], config["chat_id"], message)
    except Exception as alert_error:
        print(f"Failed to send Telegram failure alert: {alert_error}")


def build_payload(current_text, previous_text, storage, chat_ready):
    if not chat_ready:
        return {
            "ok": True,
            "status": "awaiting_chat",
            "storage": storage,
            "current_text": current_text,
            "previous_text": previous_text,
        }

    if current_text != previous_text:
        return {
            "ok": True,
            "status": "changed",
            "storage": storage,
            "old_text": previous_text,
            "new_text": current_text,
            "current_text": current_text,
        }

    return {
        "ok": True,
        "status": "unchanged",
        "storage": storage,
        "current_text": current_text,
        "previous_text": previous_text,
    }


def current_ist_timestamp():
    return datetime.now(timezone.utc).astimezone(IST).strftime(
        "%B %d, %Y at %I:%M %p IST"
    )


def parse_history_timestamp(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S IST").replace(tzinfo=IST)
    except Exception:
        return None


def load_check_history():
    try:
        response = requests.get(
            HISTORY_CSV_URL,
            headers={"Cache-Control": "no-cache"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code == 404:
            return [], 0
        response.raise_for_status()

        rows = []
        reader = csv.DictReader(StringIO(response.text))
        for row in reader:
            if row.get("timestamp"):
                rows.append(row)

        return list(reversed(rows[-MAX_HISTORY_ROWS:])), len(rows)
    except Exception:
        return [], 0


def render_status_page(payload):
    status = payload.get("status", "unknown")
    current_text = payload.get("current_text", "Unavailable")
    history_rows, total_history_rows = load_check_history()
    latest_history_time = (
        parse_history_timestamp(history_rows[0]["timestamp"]) if history_rows else None
    )
    latest_check_timestamp = (
        history_rows[0]["timestamp"] if history_rows else current_ist_timestamp()
    )
    is_monitor_stale = True
    health_badge = "Monitor stale"
    health_tone = "tone-alert"
    health_copy = (
        "Hosted checks have not reported in recently. Do not rely on alert coverage until a new row appears."
    )

    if latest_history_time is not None:
        elapsed = datetime.now(timezone.utc).astimezone(IST) - latest_history_time
        is_monitor_stale = elapsed > timedelta(minutes=STALE_AFTER_MINUTES)
        if not is_monitor_stale:
            health_badge = "Monitor healthy"
            health_tone = "tone-ok"
            health_copy = "Hosted checks are arriving on schedule."

    if status == "changed":
        badge = "Change detected"
        tone = "tone-alert"
        hero_copy = (
            "The WGAI page is now showing different entry-form text than the baseline."
        )
    elif status == "awaiting_chat":
        badge = "Monitoring live"
        tone = "tone-waiting"
        hero_copy = (
            "The public tracker is live. Telegram alerting will stay quiet until the bot chat is connected."
        )
    else:
        badge = "No change detected"
        tone = "tone-ok"
        hero_copy = (
            "The WGAI page still shows the same entry-form text as the baseline capture."
        )

    if history_rows:
        history_table_rows = "".join(
            f"""
            <tr class="history-row {'history-row-alert' if row.get('changed') == 'true' else ''}">
              <td>{escape(row.get("timestamp", ""))}</td>
              <td>{escape(row.get("status", ""))}</td>
              <td>{escape(row.get("current_text", ""))}</td>
            </tr>
            """
            for row in history_rows
        )
    else:
        history_table_rows = """
            <tr>
              <td colspan="3">Check history will appear here after the next hosted run.</td>
            </tr>
        """

    timeline_items = [
        (
            BASELINE_CAPTURED_AT,
            "Baseline captured",
            HARDCODED_INITIAL_TEXT,
        ),
        (
            MONITOR_STARTED_AT,
            "Public tracker and hosted monitor launched",
            "Hosted checks run every 5 minutes through GitHub Actions.",
        ),
        (
            current_ist_timestamp(),
            "Latest live check",
            current_text,
        ),
    ]

    timeline_html = "".join(
        f"""
        <li class="timeline-item">
          <div class="timeline-date">{escape(date_label)}</div>
          <div class="timeline-card">
            <div class="timeline-title">{escape(title)}</div>
            <div class="timeline-copy">{escape(copy)}</div>
          </div>
        </li>
        """
        for date_label, title, copy in timeline_items
    )

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="180">
    <title>WGAI Entry Form Tracker</title>
    <style>
      :root {{
        --bg: #f6f2e8;
        --panel: rgba(255, 252, 246, 0.9);
        --ink: #1f2a1f;
        --muted: #5a665a;
        --line: rgba(46, 68, 46, 0.12);
        --ok: #195c37;
        --ok-soft: #e3f3e9;
        --alert: #9d2323;
        --alert-soft: #fdebec;
        --waiting: #8a5b00;
        --waiting-soft: #fff3d8;
        --shadow: 0 20px 60px rgba(54, 48, 28, 0.12);
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: Georgia, "Times New Roman", serif;
        color: var(--ink);
        background:
          radial-gradient(circle at top left, rgba(82, 145, 94, 0.16), transparent 34%),
          radial-gradient(circle at top right, rgba(212, 161, 65, 0.18), transparent 30%),
          linear-gradient(180deg, #fbf8f1 0%, var(--bg) 100%);
      }}
      main {{
        width: min(1080px, calc(100% - 32px));
        margin: 0 auto;
        padding: 40px 0 56px;
      }}
      .hero {{
        background: linear-gradient(135deg, rgba(255,255,255,0.96), rgba(250,244,232,0.9));
        border: 1px solid var(--line);
        border-radius: 28px;
        box-shadow: var(--shadow);
        padding: 32px;
      }}
      .eyebrow {{
        font-size: 12px;
        letter-spacing: 0.16em;
        text-transform: uppercase;
        color: var(--muted);
        margin-bottom: 14px;
      }}
      h1 {{
        margin: 0 0 12px;
        font-size: clamp(34px, 6vw, 64px);
        line-height: 0.96;
      }}
      .hero-copy {{
        max-width: 720px;
        margin: 0 0 20px;
        font-size: 18px;
        color: var(--muted);
      }}
      .badge {{
        display: inline-flex;
        align-items: center;
        gap: 10px;
        border-radius: 999px;
        padding: 10px 16px;
        font-size: 14px;
        font-weight: 700;
      }}
      .tone-ok {{ background: var(--ok-soft); color: var(--ok); }}
      .tone-alert {{ background: var(--alert-soft); color: var(--alert); }}
      .tone-waiting {{ background: var(--waiting-soft); color: var(--waiting); }}
      .meta {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 16px;
        margin-top: 28px;
      }}
      .meta-card, .text-card, .timeline-card {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 22px;
        box-shadow: 0 16px 40px rgba(54, 48, 28, 0.06);
      }}
      .meta-card {{
        padding: 18px 20px;
      }}
      .meta-label {{
        font-size: 12px;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        color: var(--muted);
        margin-bottom: 8px;
      }}
      .meta-value {{
        font-size: 20px;
        line-height: 1.3;
      }}
      .grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
        gap: 18px;
        margin-top: 22px;
      }}
      .text-card {{
        padding: 22px;
      }}
      .text-label {{
        font-size: 13px;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        color: var(--muted);
        margin-bottom: 12px;
      }}
      .text-value {{
        font-size: 24px;
        line-height: 1.3;
      }}
      .section-title {{
        margin: 34px 0 16px;
        font-size: 24px;
      }}
      .timeline {{
        list-style: none;
        padding: 0;
        margin: 0;
        display: grid;
        gap: 14px;
      }}
      .timeline-item {{
        display: grid;
        grid-template-columns: 180px 1fr;
        gap: 14px;
        align-items: start;
      }}
      .timeline-date {{
        padding-top: 14px;
        color: var(--muted);
        font-size: 14px;
      }}
      .timeline-card {{
        padding: 16px 18px;
      }}
      .timeline-title {{
        font-size: 18px;
        margin-bottom: 6px;
      }}
      .timeline-copy {{
        color: var(--muted);
      }}
      .footer {{
        margin-top: 24px;
        color: var(--muted);
        font-size: 14px;
      }}
      .table-wrap {{
        overflow-x: auto;
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 22px;
        box-shadow: 0 16px 40px rgba(54, 48, 28, 0.06);
      }}
      .history-table {{
        width: 100%;
        border-collapse: collapse;
        min-width: 760px;
      }}
      .history-table th,
      .history-table td {{
        padding: 14px 16px;
        text-align: left;
        border-bottom: 1px solid var(--line);
        vertical-align: top;
      }}
      .history-table th {{
        font-size: 12px;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        color: var(--muted);
        background: rgba(255, 255, 255, 0.55);
      }}
      .history-row-alert td {{
        background: rgba(253, 235, 236, 0.6);
      }}
      a {{ color: inherit; }}
      @media (max-width: 700px) {{
        .hero {{ padding: 24px; border-radius: 22px; }}
        .timeline-item {{ grid-template-columns: 1fr; gap: 8px; }}
        .timeline-date {{ padding-top: 0; }}
      }}
    </style>
  </head>
  <body>
    <main>
      <section class="hero">
        <div class="eyebrow">Shareable Live Tracker</div>
        <h1>WGAI Entry Form Status</h1>
        <p class="hero-copy">{escape(hero_copy)}</p>
        <div class="badge {tone}">{escape(badge)}</div>

        <div class="meta">
          <article class="meta-card">
            <div class="meta-label">Monitor Health</div>
            <div class="meta-value">
              <span class="badge {health_tone}">{escape(health_badge)}</span>
            </div>
            <div class="timeline-copy" style="margin-top: 10px;">{escape(health_copy)}</div>
          </article>
          <article class="meta-card">
            <div class="meta-label">Latest Hosted Check</div>
            <div class="meta-value">{escape(latest_check_timestamp)}</div>
          </article>
          <article class="meta-card">
            <div class="meta-label">Check Frequency</div>
            <div class="meta-value">Every 5 minutes</div>
          </article>
          <article class="meta-card">
            <div class="meta-label">What We Watch</div>
            <div class="meta-value">Any short text containing “Entry Form for Amateur Players”</div>
          </article>
          <article class="meta-card">
            <div class="meta-label">Checks Logged</div>
            <div class="meta-value">{total_history_rows}</div>
          </article>
        </div>

        <div class="grid">
          <article class="text-card">
            <div class="text-label">Baseline Text</div>
            <div class="text-value">{escape(HARDCODED_INITIAL_TEXT)}</div>
          </article>
          <article class="text-card">
            <div class="text-label">Current Live Text</div>
            <div class="text-value">{escape(current_text)}</div>
          </article>
        </div>
      </section>

      <h2 class="section-title">Tracking History</h2>
      <ol class="timeline">
        {timeline_html}
      </ol>

      <h2 class="section-title">Check Log</h2>
      <div class="table-wrap">
        <table class="history-table">
          <thead>
            <tr>
              <th>Timestamp of Check</th>
              <th>Status</th>
              <th>Observed Text</th>
            </tr>
          </thead>
          <tbody>
            {history_table_rows}
          </tbody>
        </table>
      </div>

      <p class="footer">
        Source page:
        <a href="{escape(MONITOR_URL)}">{escape(MONITOR_URL)}</a>
      </p>
      <p class="footer">
        Public page hosted on Vercel. Automated checks run on GitHub Actions every 5 minutes, and Telegram alerts are sent only for real changes or monitor failures.
      </p>
    </main>
  </body>
</html>"""


# The core monitoring flow is kept separate so it can be tested locally.
def run_check(send_notifications=True):
    config = load_config()

    try:
        if send_notifications and not config["telegram_token"]:
            raise RuntimeError("Missing required environment variable: TELEGRAM_TOKEN")

        if send_notifications and not config["chat_id"]:
            config["chat_id"] = detect_chat_id(config["telegram_token"])

        current_text = fetch_current_text()
        use_kv = bool(config["kv_rest_api_url"] and config["kv_rest_api_token"])

        if use_kv:
            previous_text = kv_get(
                config["kv_rest_api_url"], config["kv_rest_api_token"], KV_KEY
            )
            is_first_run = previous_text is None
        else:
            previous_text = HARDCODED_INITIAL_TEXT
            is_first_run = False

        if not send_notifications:
            return 200, build_payload(
                current_text,
                previous_text,
                "kv" if use_kv else "baseline",
                True,
            )

        if not config["chat_id"]:
            if use_kv and previous_text is None:
                kv_set(
                    config["kv_rest_api_url"],
                    config["kv_rest_api_token"],
                    KV_KEY,
                    current_text,
                )

            return 200, {
                "ok": True,
                "status": "awaiting_chat",
                "storage": "kv" if use_kv else "baseline",
                "current_text": current_text,
                "previous_text": previous_text,
            }

        if is_first_run:
            send_telegram_message(
                config["telegram_token"],
                config["chat_id"],
                "WGAI monitor is live.\n\n"
                "First observed text:\n"
                f"{current_text}\n\n"
                f"URL:\n{MONITOR_URL}",
            )

            if use_kv:
                kv_set(
                    config["kv_rest_api_url"],
                    config["kv_rest_api_token"],
                    KV_KEY,
                    current_text,
                )

            return 200, {
                "ok": True,
                "status": "initialized",
                "storage": "kv" if use_kv else "baseline",
                "current_text": current_text,
            }

        if current_text != previous_text:
            send_telegram_message(
                config["telegram_token"],
                config["chat_id"],
                "WGAI text changed.\n\n"
                "Old text:\n"
                f"{previous_text}\n\n"
                "New text:\n"
                f"{current_text}\n\n"
                f"URL:\n{MONITOR_URL}",
            )

            if use_kv:
                kv_set(
                    config["kv_rest_api_url"],
                    config["kv_rest_api_token"],
                    KV_KEY,
                    current_text,
                )

            return 200, {
                "ok": True,
                "status": "changed",
                "storage": "kv" if use_kv else "baseline",
                "old_text": previous_text,
                "new_text": current_text,
            }

        return 200, {
            "ok": True,
            "status": "unchanged",
            "storage": "kv" if use_kv else "baseline",
            "current_text": current_text,
        }
    except Exception as error:
        send_failure_alert(config, str(error))
        return 500, {"ok": False, "status": "error", "error": str(error)}


# Vercel's Python runtime invokes this handler class for incoming requests.
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        confirm = query.get("confirm", ["0"])[0] == "1"
        send_notifications = parsed.path == "/api/notify"
        status_code, payload = run_check(send_notifications=send_notifications)

        if confirm and payload.get("ok") and payload.get("status") == "unchanged":
            config = load_config()
            if not config["chat_id"]:
                config["chat_id"] = detect_chat_id(config["telegram_token"])

            if config["chat_id"]:
                try:
                    send_telegram_message(
                        config["telegram_token"],
                        config["chat_id"],
                        "WGAI monitor is live.\n\n"
                        "Current text:\n"
                        f"{payload['current_text']}\n\n"
                        f"URL:\n{MONITOR_URL}",
                    )
                    payload["status"] = "confirmed"
                except Exception as error:
                    payload = {
                        "ok": False,
                        "status": "error",
                        "error": str(error),
                    }
                    status_code = 500

        if parsed.path == "/":
            self.send_response(status_code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(render_status_page(payload).encode("utf-8"))
            return

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))
