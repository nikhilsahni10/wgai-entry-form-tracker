import csv
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup


# The GitHub Actions monitor uses the same matching rules as the public page.
MONITOR_URL = "https://www.wgai.co.in/pages/membership-information.php"
TARGET_SUBSTRING = "Entry Form for Amateur Players"
ENTRY_FORM_PATTERN = re.compile(
    r"Entry Form for Amateur Players"
    r"(?:\s*[-\u2013\u2014]\s*Season\s+\d{4}(?:\s*\([^)]*Leg[^)]*\))?)?",
    re.IGNORECASE,
)
MATCH_SEPARATOR = " | "
REQUEST_TIMEOUT_SECONDS = 20
FETCH_RETRIES = 3
FALLBACK_FETCH_RETRIES = 2
TELEGRAM_RETRIES = 5
STATE_PATH = ".state/last_text.txt"
FETCH_SOURCE_STATE_PATH = ".state/fetch_source.txt"
HISTORY_PATH = "data/check_history.csv"
DEFAULT_TRACKER_URL = "https://wgai-monitor.vercel.app"
FETCH_SOURCE_DIRECT = "direct"
FETCH_SOURCE_TRACKER_FALLBACK = "tracker_fallback"
IST = timezone(timedelta(hours=5, minutes=30))


def normalize_public_url(value):
    value = value.strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    return value


def public_tracker_url():
    for env_name in (
        "TRACKER_URL",
        "PUBLIC_TRACKER_URL",
        "VERCEL_PROJECT_PRODUCTION_URL",
        "VERCEL_URL",
    ):
        tracker_url = normalize_public_url(os.environ.get(env_name, ""))
        if tracker_url:
            return tracker_url

    return DEFAULT_TRACKER_URL


TRACKER_URL = public_tracker_url()
TRACKER_API_URL = f"{TRACKER_URL}/api/check"


def normalize_text(value):
    return " ".join(value.split())


def extract_target_texts(html):
    soup = BeautifulSoup(html, "html.parser")
    matches = []
    seen = set()

    page_text = soup.get_text("\n", strip=True)
    for line in page_text.splitlines():
        for match in ENTRY_FORM_PATTERN.finditer(normalize_text(line)):
            text = normalize_text(match.group(0))
            key = text.casefold()
            if TARGET_SUBSTRING.casefold() in key and key not in seen:
                seen.add(key)
                matches.append(text)

    if not matches:
        raise ValueError(
            f'No entry-form text containing "{TARGET_SUBSTRING}" was found.'
        )

    return matches


def extract_target_text(html):
    return MATCH_SEPARATOR.join(extract_target_texts(html))


def fetch_direct_current_text():
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


def fetch_tracker_current_text():
    last_error = None

    for attempt in range(1, FALLBACK_FETCH_RETRIES + 1):
        try:
            response = requests.get(
                TRACKER_API_URL,
                headers={"User-Agent": "Mozilla/5.0 (compatible; WGAITextMonitor/1.0)"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()

            if not payload.get("ok"):
                raise RuntimeError(payload.get("error") or f"Tracker API error: {payload}")

            if payload.get("live") is False or payload.get("status") == "source_unavailable":
                raise RuntimeError(
                    payload.get("error")
                    or "Tracker API returned a previously recorded observation, not live data."
                )

            current_text = normalize_text(payload.get("current_text", ""))
            if not current_text:
                raise RuntimeError("Tracker API returned an empty current_text value.")

            return current_text
        except Exception as error:
            last_error = error
            if attempt < FALLBACK_FETCH_RETRIES:
                time.sleep(3)

    raise RuntimeError(
        "Tracker fallback failed after "
        f"{FALLBACK_FETCH_RETRIES} attempts: {last_error}"
    )


def fetch_current_text():
    try:
        current_text = fetch_direct_current_text()
        return current_text, FETCH_SOURCE_DIRECT, None
    except Exception as direct_error:
        try:
            current_text = fetch_tracker_current_text()
            return current_text, FETCH_SOURCE_TRACKER_FALLBACK, direct_error
        except Exception as fallback_error:
            raise RuntimeError(
                "Direct fetch failed and tracker fallback also failed: "
                f"direct_error={direct_error}; "
                f"fallback_error={fallback_error}"
            ) from fallback_error


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
            lineterminator="\n",
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


def load_previous_fetch_source():
    os.makedirs(os.path.dirname(FETCH_SOURCE_STATE_PATH), exist_ok=True)

    if not os.path.exists(FETCH_SOURCE_STATE_PATH):
        return ""

    with open(FETCH_SOURCE_STATE_PATH, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def save_fetch_source(fetch_source):
    os.makedirs(os.path.dirname(FETCH_SOURCE_STATE_PATH), exist_ok=True)
    with open(FETCH_SOURCE_STATE_PATH, "w", encoding="utf-8") as fh:
        fh.write(fetch_source)


def main():
    telegram_token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()
    timestamp = current_ist_timestamp()
    previous_text, initialized = load_previous_text()
    current_text = ""
    fetch_source = ""
    direct_error = None

    try:
        if not telegram_token or not chat_id:
            raise RuntimeError(
                "Missing TELEGRAM_TOKEN or CHAT_ID in GitHub Actions secrets."
            )

        current_text, fetch_source, direct_error = fetch_current_text()
        changed = initialized and current_text != previous_text

        if changed:
            message = (
                "WGAI text changed.\n\n"
                "Old text:\n"
                f"{previous_text}\n\n"
                "New text:\n"
                f"{current_text}\n\n"
                "Public tracker:\n"
                f"{TRACKER_URL}"
            )
            if fetch_source == FETCH_SOURCE_TRACKER_FALLBACK and direct_error is not None:
                message += (
                    "\n\n"
                    "Observed via tracker fallback because the direct GitHub Actions "
                    "fetch failed.\n\n"
                    "Direct fetch error:\n"
                    f"{truncate_for_history(str(direct_error), limit=400)}"
                )
            send_telegram_message(
                telegram_token,
                chat_id,
                message,
            )

        save_current_text(current_text)
        save_fetch_source(fetch_source)

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

        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
