import os
import threading
import time
import requests
import praw
from flask import Flask
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

# --- Config ---
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))
DISCORD_TIMEOUT = float(os.getenv("DISCORD_TIMEOUT", "6"))  # seconds
BACKFILL_LIMIT = int(os.getenv("BACKFILL_LIMIT", "500"))  # how many posts to scan on startup

# --- Load environment variables ---
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
DISCORD_PING_USER_ID = os.getenv("DISCORD_PING_USER_ID")

# --- Reddit setup ---
reddit = praw.Reddit(
    client_id=REDDIT_CLIENT_ID,
    client_secret=REDDIT_CLIENT_SECRET,
    user_agent=REDDIT_USER_AGENT
)
subreddit = reddit.subreddit("plsdonategame")
TARGET_FLAIRS = {"Free Giveaway", "Requirement Giveaway"}

# --- Deduplication and locking ---
sent_posts = set()
sent_lock = Lock()

# --- Thread pool for non-blocking webhook sends ---
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

def log(msg):
    print(f"[{datetime.now(timezone.utc).astimezone().isoformat()}] {msg}")

def send_webhook_payload(payload):
    """Send to Discord with basic 429 handling and timeout."""
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=DISCORD_TIMEOUT)
        if r.status_code == 204 or r.status_code == 200:
            return True, None
        if r.status_code == 429:
            # Respect Retry-After header if present
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else 5.0
            log(f"Discord rate limited. Waiting {wait}s then retrying.")
            time.sleep(wait)
            r2 = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=DISCORD_TIMEOUT)
            if r2.status_code in (200, 204):
                return True, None
            return False, f"Discord returned {r2.status_code}"
        return False, f"Discord returned {r.status_code}"
    except Exception as e:
        return False, str(e)

def send_to_discord_async(submission):
    """Worker task: send webhook and mark as sent on success."""
    payload = {
        "content": f"{('<@' + DISCORD_PING_USER_ID + '> ') if DISCORD_PING_USER_ID else ''}🎉 New giveaway!\n**{submission.title}**\nhttps://reddit.com{submission.permalink}"
    }
    success, err = send_webhook_payload(payload)
    if success:
        with sent_lock:
            sent_posts.add(submission.id)
        log(f"Delivered: {submission.id} | {submission.title}")
    else:
        log(f"Failed to deliver {submission.id}: {err}")

def schedule_send(submission):
    """Schedule a send if not already sent. Non-blocking."""
    with sent_lock:
        if submission.id in sent_posts:
            return
    # Submit to thread pool; do not block the stream
    executor.submit(send_to_discord_async, submission)

def catch_recent_posts():
    """Backfill posts from the last 24 hours with target flair."""
    log("Backfilling posts from last 24 hours...")
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    count_checked = 0
    for submission in subreddit.new(limit=BACKFILL_LIMIT):
        count_checked += 1
        created = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)
        # Log creation vs now to measure delay
        seen_time = datetime.now(timezone.utc)
        age_seconds = (seen_time - created).total_seconds()
        log(f"Backfill saw: {submission.id} | flair={submission.link_flair_text} | age={int(age_seconds)}s | title={submission.title}")
        if created >= cutoff:
            flair = submission.link_flair_text
            if flair and flair.strip().lower() in {f.lower() for f in TARGET_FLAIRS}:
                schedule_send(submission)
    log(f"Backfill checked {count_checked} posts.")

def reddit_stream():
    """Stream new posts continuously with retry loop and immediate scheduling."""
    log("Starting stream...")
    while True:
        try:
            for submission in subreddit.stream.submissions(skip_existing=True):
                # Immediately log when we see the post and compute latency
                created = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)
                seen_time = datetime.now(timezone.utc)
                latency = (seen_time - created).total_seconds()
                log(f"Stream saw: {submission.id} | flair={submission.link_flair_text} | latency={int(latency)}s | title={submission.title}")
                flair = submission.link_flair_text
                if flair and flair.strip().lower() in {f.lower() for f in TARGET_FLAIRS}:
                    # schedule non-blocking send
                    schedule_send(submission)
        except Exception as e:
            log(f"Stream error: {e}. Restarting stream in 5s.")
            time.sleep(5)

# --- Flask app to keep Render alive ---
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    # Optionally load persisted sent_posts here (not included)
    catch_recent_posts()
    threading.Thread(target=reddit_stream, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
