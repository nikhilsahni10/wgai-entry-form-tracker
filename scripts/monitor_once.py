import csv
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup


# The GitHub Actions monitor uses the same matching rules as the public page.
MONITOR_URL = "https://www.wgai.co.in/pages/membership-information.php"
TARGET_SUBSTRING = "Entry Form for Amateur Players"
MAX_MATCH_LENGTH = 100
REQUEST_TIMEOUT_SECONDS = 20
FETCH_RETRIES = 3
TELEGRAM_RETRIES = 5
STATE_PATH = ".state/last_text.txt"
HISTORY_PATH = "data/check_history.csv"
TRACKER_URL = "https://wgai-monitor.vercel.app/"
IST = timezone(timedelta(hours=5, minutes=30))


def normalize_text(value):
    return " ".join(value.split())


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


def fetch_current_text():
    last_error = None

    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            response = requests.get(
                MONITOR_URL,
                headers={"User-Agent": "Mozilla/5.0 (compatible; WGAITextMonitor/1.0)"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return extract_target_text(response.text)
        except Exception as error:
            last_error = error
            if attempt < FETCH_RETRIES:
                time.sleep(5)

    raise RuntimeError(
        f"Failed to fetch monitored text after {FETCH_RETRIES} attempts: {last_error}"
    )


def send_telegram_message(token, chat_id, message):
    last_error = None

    for attempt in range(1, TELEGRAM_RETRIES + 1):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": message},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(f"Telegram API error: {payload}")
            return
        except Exception as error:
            last_error = error
            if attempt < TELEGRAM_RETRIES:
                time.sleep(4)

    raise RuntimeError(
        f"Telegram send failed after {TELEGRAM_RETRIES} attempts: {last_error}"
    )


def current_ist_timestamp():
    return datetime.now(timezone.utc).astimezone(IST).strftime(
        "%Y-%m-%d %H:%M:%S IST"
    )


def append_history_row(timestamp, status, changed, current_text):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    history_exists = os.path.exists(HISTORY_PATH)

    with open(HISTORY_PATH, "a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["timestamp", "status", "changed", "current_text"],
        )
        if not history_exists:
            writer.writeheader()
        writer.writerow(
            {
                "timestamp": timestamp,
                "status": status,
                "changed": "true" if changed else "false",
                "current_text": current_text,
            }
        )


def truncate_for_history(value, limit=220):
    normalized = normalize_text(value)
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def load_previous_text():
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)

    if not os.path.exists(STATE_PATH):
        return "", False

    with open(STATE_PATH, "r", encoding="utf-8") as fh:
        return fh.read().strip(), True


def save_current_text(current_text):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        fh.write(current_text)


def main():
    telegram_token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()
    timestamp = current_ist_timestamp()
    previous_text, initialized = load_previous_text()
    current_text = ""

    if not telegram_token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_TOKEN or CHAT_ID in GitHub Actions secrets.")

    try:
        current_text = fetch_current_text()
        changed = initialized and current_text != previous_text

        if changed:
            send_telegram_message(
                telegram_token,
                chat_id,
                "WGAI text changed.\n\n"
                "Old text:\n"
                f"{previous_text}\n\n"
                "New text:\n"
                f"{current_text}\n\n"
                "Public tracker:\n"
                f"{TRACKER_URL}",
            )

        save_current_text(current_text)

        if not initialized:
            status = "initialized"
        elif changed:
            status = "changed"
        else:
            status = "unchanged"

        append_history_row(timestamp, status, changed, current_text)
    except Exception as error:
        error_summary = truncate_for_history(str(error))
        failure_text = f"Monitoring failed: {error_summary}"
        append_history_row(timestamp, "failed", False, failure_text)

        try:
            send_telegram_message(
                telegram_token,
                chat_id,
                "WGAI monitor failed.\n\n"
                f"Time:\n{timestamp}\n\n"
                f"Reason:\n{error_summary}\n\n"
                f"Latest observed text:\n{current_text or 'Unavailable'}\n\n"
                "Public tracker:\n"
                f"{TRACKER_URL}",
            )
        except Exception as alert_error:
            print(f"Failed to send Telegram failure alert: {alert_error}", file=sys.stderr)

        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
