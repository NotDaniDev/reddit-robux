import os
import threading
import time
import requests
import praw
from flask import Flask
from datetime import datetime, timezone, timedelta

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

# --- Track sent posts to avoid duplicates ---
sent_posts = set()

def send_to_discord(submission):
    if submission.id in sent_posts:
        return
    post_url = f"https://reddit.com{submission.permalink}"
    ping = f"<@{DISCORD_PING_USER_ID}> " if DISCORD_PING_USER_ID else ""
    data = {"content": f"{ping}🎉 New giveaway!\n**{submission.title}**\n{post_url}"}
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=data)
        print("Sent:", submission.title, "| Discord response:", r.status_code)
        if r.status_code == 204:
            sent_posts.add(submission.id)
    except Exception as e:
        print("Error sending to Discord:", e)

def catch_recent_posts():
    """Backfill posts from the last 24 hours with target flair."""
    print("Checking for posts from the last 24 hours...")
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    for submission in subreddit.new(limit=200):  # scan more posts
        created = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)
        print("Saw post:", submission.title, "| Flair:", submission.link_flair_text, "| Created:", created)
        if created >= cutoff:
            flair = submission.link_flair_text
            if flair and flair.strip().lower() in {f.lower() for f in TARGET_FLAIRS}:
                send_to_discord(submission)

def reddit_stream():
    """Stream new posts continuously with retry loop."""
    print("Streaming new posts...")
    while True:
        try:
            for submission in subreddit.stream.submissions(skip_existing=True):
                print("Saw post:", submission.title, "| Flair:", submission.link_flair_text)
                flair = submission.link_flair_text
                if flair and flair.strip().lower() in {f.lower() for f in TARGET_FLAIRS}:
                    send_to_discord(submission)
        except Exception as e:
            print("Stream error:", e)
            time.sleep(10)  # wait before retrying

# --- Flask app to keep Render alive ---
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    # First, catch posts from last 24h
    catch_recent_posts()
    # Then start streaming in background
    threading.Thread(target=reddit_stream, daemon=True).start()
    # Run Flask server on Render’s required port
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
