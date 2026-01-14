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
SUBREDDITS = ["plsdonategame", "FreeRobloxAccounts2"]  # add more here if needed

# --- Per-subreddit flair targets ---
FLAIR_MAP = {
    "plsdonategame": {"Free Giveaway", "Requirement Giveaway"},
    "FreeRobloxAccounts2": {"Free Account", "Account Giveaway"}
}

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

# Create subreddit objects for backfill; stream will use combined name
subreddit_objs = {name: reddit.subreddit(name) for name in SUBREDDITS}
combined_subreddits = "+".join(SUBREDDITS)

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
        if r.status_code in (204, 200):
            return True, None
        if r.status_code == 429:
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
    executor.submit(send_to_discord_async, submission)

def matches_target_flair(submission):
    """Return True if submission's flair matches the target set for its subreddit."""
    flair = submission.link_flair_text
    if not flair:
        return False
    flair_norm = flair.strip().lower()
    sub_name = submission.subreddit.display_name  # exact subreddit name
    target_set = FLAIR_MAP.get(sub_name)
    if not target_set:
        # fallback: check all flair sets if subreddit not explicitly listed
        combined = set().union(*FLAIR_MAP.values())
        return flair_norm in combined
    return flair_norm in target_set

def catch_recent_posts():
    """Backfill posts from the last 24 hours with target flair for each subreddit."""
    log("Backfilling posts from last 24 hours for each subreddit...")
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    for name, sub in subreddit_objs.items():
        count_checked = 0
        log(f"Backfilling subreddit: {name}")
        for submission in sub.new(limit=BACKFILL_LIMIT):
            count_checked += 1
            created = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - created).total_seconds()
            log(f"[{name}] saw: {submission.id} | flair={submission.link_flair_text} | age={int(age_seconds)}s | title={submission.title}")
            if created >= cutoff and matches_target_flair(submission):
                schedule_send(submission)
        log(f"[{name}] backfill checked {count_checked} posts.")

def reddit_stream():
    """Stream new posts continuously with retry loop and immediate scheduling."""
    log(f"Starting combined stream for: {combined_subreddits}")
    while True:
        try:
            for submission in reddit.subreddit(combined_subreddits).stream.submissions(skip_existing=True):
                created = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)
                latency = (datetime.now(timezone.utc) - created).total_seconds()
                log(f"Stream saw: {submission.id} | sub={submission.subreddit.display_name} | flair={submission.link_flair_text} | latency={int(latency)}s | title={submission.title}")
                if matches_target_flair(submission):
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
