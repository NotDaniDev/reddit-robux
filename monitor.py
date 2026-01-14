# main.py
import os
import threading
import time
import json
import requests
import praw
from flask import Flask, jsonify
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

# --- Configurable settings ---
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "6"))
DISCORD_TIMEOUT = float(os.getenv("DISCORD_TIMEOUT", "6"))
BACKFILL_LIMIT = int(os.getenv("BACKFILL_LIMIT", "1000"))
RESCAN_INTERVAL_SECONDS = int(os.getenv("RESCAN_INTERVAL_SECONDS", "120"))  # every 2 minutes
RESCAN_WINDOW_HOURS = int(os.getenv("RESCAN_WINDOW_HOURS", "24"))
RESCAN_LIMIT = int(os.getenv("RESCAN_LIMIT", "1000"))
PERSIST_FILE = os.getenv("PERSIST_FILE", "sent_posts.json")
WATCHDOG_INTERVAL = int(os.getenv("WATCHDOG_INTERVAL", "30"))

# --- Subreddits and flair mapping ---
SUBREDDITS = ["plsdonategame", "FreeRobloxAccounts2"]
FLAIR_MAP = {
    "plsdonategame": {"free giveaway", "requirement giveaway"},
    "FreeRobloxAccounts2": {"free account", "account giveaway"}
}

# --- Env vars ---
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
DISCORD_PING_USER_ID = os.getenv("DISCORD_PING_USER_ID")  # ping only for posts

# --- Reddit setup ---
reddit = praw.Reddit(
    client_id=REDDIT_CLIENT_ID,
    client_secret=REDDIT_CLIENT_SECRET,
    user_agent=REDDIT_USER_AGENT
)

subreddit_objs = {name: reddit.subreddit(name) for name in SUBREDDITS}
combined_subreddits = "+".join(SUBREDDITS)

# --- State, locking, thread pool ---
sent_posts = set()
sent_lock = Lock()
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# --- Runtime stats ---
stats = {
    "delivered": 0,
    "failed": 0,
    "seen_stream": 0,
    "seen_rescan": 0,
    "last_stream_error": None,
    "last_rescan_error": None
}

# --- In-memory recent logs buffer (for quick dump if needed) ---
RECENT_LOGS_MAX = 200
recent_logs = []
recent_logs_lock = Lock()

def now_ts():
    return datetime.now(timezone.utc).astimezone().isoformat()

def buffer_log(entry):
    with recent_logs_lock:
        recent_logs.append(entry)
        if len(recent_logs) > RECENT_LOGS_MAX:
            recent_logs.pop(0)

# --- Discord log sending (no ping) ---
def send_log_payload(payload):
    """Send a log message to Discord webhook (no ping). Returns (success, err)."""
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=DISCORD_TIMEOUT)
        if r.status_code in (200, 204):
            return True, None
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else 5.0
            time.sleep(wait)
            r2 = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=DISCORD_TIMEOUT)
            if r2.status_code in (200, 204):
                return True, None
            return False, f"Discord returned {r2.status_code}"
        return False, f"Discord returned {r.status_code}"
    except Exception as e:
        return False, str(e)

def send_log_to_discord_async(level, message):
    """Send a log message to Discord without pinging. Runs in thread pool."""
    payload = {
        "username": "Bot Logs",
        "content": f"[{level}] {message}"
    }
    success, err = send_log_payload(payload)
    if not success:
        # If log sending fails, still print locally
        print(f"[{now_ts()}] Failed to send log to Discord: {err}", flush=True)

# --- General logging helpers ---
def log_info(msg, send_to_discord=False):
    entry = f"[{now_ts()}] INFO: {msg}"
    print(entry, flush=True)
    buffer_log(entry)
    if send_to_discord:
        executor.submit(send_log_to_discord_async, "INFO", msg)

def log_error(msg, send_to_discord=True):
    entry = f"[{now_ts()}] ERROR: {msg}"
    print(entry, flush=True)
    buffer_log(entry)
    if send_to_discord:
        # send errors to discord without ping
        executor.submit(send_log_to_discord_async, "ERROR", msg)

# --- Persistence helpers ---
def load_sent_posts():
    global sent_posts
    try:
        if os.path.exists(PERSIST_FILE):
            with open(PERSIST_FILE, "r") as f:
                data = json.load(f)
                sent_posts = set(data.get("ids", []))
                log_info(f"Loaded {len(sent_posts)} persisted sent IDs")
    except Exception as e:
        log_error(f"Error loading persist file: {e}")

def persist_sent_posts():
    try:
        with sent_lock:
            data = {"ids": list(sent_posts)}
        with open(PERSIST_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log_error(f"Error persisting sent posts: {e}")

# --- Robust flair extraction ---
def get_flair_text(submission):
    if getattr(submission, "link_flair_text", None):
        return submission.link_flair_text
    rich = getattr(submission, "link_flair_richtext", None)
    if rich and isinstance(rich, list):
        parts = []
        for item in rich:
            if isinstance(item, dict):
                text = item.get("t") or item.get("text")
                if text:
                    parts.append(text)
        if parts:
            return " ".join(parts)
    if getattr(submission, "author_flair_text", None):
        return submission.author_flair_text
    return None

def matches_target_flair(submission):
    flair = get_flair_text(submission)
    if not flair:
        return False
    flair_norm = flair.strip().lower()
    sub_name = submission.subreddit.display_name
    target_set = FLAIR_MAP.get(sub_name)
    if not target_set:
        combined = set().union(*FLAIR_MAP.values())
        return flair_norm in combined
    return flair_norm in target_set

# --- Discord sending for posts (pings allowed) ---
def send_webhook_payload(payload):
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=DISCORD_TIMEOUT)
        if r.status_code in (200, 204):
            return True, None
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else 5.0
            time.sleep(wait)
            r2 = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=DISCORD_TIMEOUT)
            if r2.status_code in (200, 204):
                return True, None
            return False, f"Discord returned {r2.status_code}"
        return False, f"Discord returned {r.status_code}"
    except Exception as e:
        return False, str(e)

def send_to_discord_async(submission):
    ping = f"<@{DISCORD_PING_USER_ID}> " if DISCORD_PING_USER_ID else ""
    payload = {
        "content": f"{ping}🎉 New giveaway!\n**{submission.title}**\nhttps://reddit.com{submission.permalink}"
    }
    success, err = send_webhook_payload(payload)
    if success:
        with sent_lock:
            sent_posts.add(submission.id)
        persist_sent_posts()
        stats["delivered"] += 1
        log_info(f"Delivered: {submission.id} | {submission.title}")
    else:
        stats["failed"] += 1
        log_error(f"Failed to deliver {submission.id}: {err}")

def schedule_send(submission):
    with sent_lock:
        if submission.id in sent_posts:
            return
    executor.submit(send_to_discord_async, submission)

# --- Backfill on startup ---
def catch_recent_posts():
    log_info("Backfilling posts from last 24 hours for each subreddit...")
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    for name, sub in subreddit_objs.items():
        count_checked = 0
        log_info(f"Backfilling subreddit: {name}")
        try:
            for submission in sub.new(limit=BACKFILL_LIMIT):
                count_checked += 1
                created = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)
                age_seconds = (datetime.now(timezone.utc) - created).total_seconds()
                log_info(f"[{name}] backfill saw: {submission.id} | flair={get_flair_text(submission)} | age={int(age_seconds)}s | title={submission.title}")
                if created >= cutoff and matches_target_flair(submission):
                    schedule_send(submission)
        except Exception as e:
            log_error(f"Backfill error for {name}: {e}")
        log_info(f"[{name}] backfill checked {count_checked} posts")

# --- Stream with retry and robust logging ---
stream_thread_ref = {"thread": None}

def reddit_stream():
    log_info(f"Starting combined stream for: {combined_subreddits}")
    while True:
        try:
            for submission in reddit.subreddit(combined_subreddits).stream.submissions(skip_existing=True):
                stats["seen_stream"] += 1
                created = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)
                latency = (datetime.now(timezone.utc) - created).total_seconds()
                log_info(f"Stream saw: {submission.id} | sub={submission.subreddit.display_name} | flair={get_flair_text(submission)} | latency={int(latency)}s | title={submission.title}")
                if matches_target_flair(submission):
                    schedule_send(submission)
        except Exception as e:
            stats["last_stream_error"] = str(e)
            log_error(f"Stream error: {e}")
            time.sleep(5)

# --- Periodic rescan to catch flairs applied later ---
def periodic_rescan():
    log_info(f"Starting periodic rescan every {RESCAN_INTERVAL_SECONDS}s for last {RESCAN_WINDOW_HOURS}h")
    while True:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=RESCAN_WINDOW_HOURS)
            for name, sub in subreddit_objs.items():
                checked = 0
                log_info(f"[rescan] scanning subreddit: {name}")
                for submission in sub.new(limit=RESCAN_LIMIT):
                    checked += 1
                    created = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)
                    if created < cutoff:
                        break
                    with sent_lock:
                        already = submission.id in sent_posts
                    if already:
                        continue
                    stats["seen_rescan"] += 1
                    log_info(f"[rescan][{name}] saw: {submission.id} | flair={get_flair_text(submission)} | created={created.isoformat()} | title={submission.title}")
                    if matches_target_flair(submission):
                        log_info(f"[rescan] scheduling send for {submission.id} (flair matched on rescan)")
                        schedule_send(submission)
                log_info(f"[rescan][{name}] checked {checked} posts")
        except Exception as e:
            stats["last_rescan_error"] = str(e)
            log_error(f"Rescan error: {e}")
        time.sleep(RESCAN_INTERVAL_SECONDS)

# --- Watchdog to ensure stream thread is alive ---
def start_stream_thread():
    t = threading.Thread(target=reddit_stream, daemon=True)
    stream_thread_ref["thread"] = t
    t.start()
    return t

def watchdog():
    log_info("Starting watchdog")
    while True:
        t = stream_thread_ref.get("thread")
        if t is None or not t.is_alive():
            log_error("Stream thread not alive; restarting")
            start_stream_thread()
        time.sleep(WATCHDOG_INTERVAL)

# --- Flask health endpoint ---
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

@app.route("/health")
def health():
    with sent_lock:
        delivered = len(sent_posts)
    with recent_logs_lock:
        logs_snapshot = list(recent_logs)
    return jsonify({
        "delivered_count": delivered,
        "delivered_stat": stats["delivered"],
        "failed_stat": stats["failed"],
        "seen_stream": stats["seen_stream"],
        "seen_rescan": stats["seen_rescan"],
        "last_stream_error": stats["last_stream_error"],
        "last_rescan_error": stats["last_rescan_error"],
        "recent_logs": logs_snapshot[-20:]  # include last 20 logs for quick inspection
    })

if __name__ == "__main__":
    load_sent_posts()
    catch_recent_posts()
    start_stream_thread()
    threading.Thread(target=periodic_rescan, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    # Persist periodically in background
    def periodic_persist():
        while True:
            persist_sent_posts()
            time.sleep(60)
    threading.Thread(target=periodic_persist, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
