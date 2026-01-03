import os
import time
import threading
import requests
import praw
from flask import Flask

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
seen_posts = set()

def send_to_discord(submission):
    post_url = f"https://reddit.com{submission.permalink}"
    ping = f"<@{DISCORD_PING_USER_ID}> " if DISCORD_PING_USER_ID else ""
    data = {"content": f"{ping}🎉 New giveaway!\n**{submission.title}**\n{post_url}"}
    r = requests.post(DISCORD_WEBHOOK_URL, json=data)
    print("Discord response:", r.status_code)

def reddit_loop():
    print("Bot started. Monitoring r/plsdonategame...")
    while True:
        for submission in subreddit.new(limit=5):
            if submission.id not in seen_posts and submission.link_flair_text in TARGET_FLAIRS:
                seen_posts.add(submission.id)
                send_to_discord(submission)
        time.sleep(30)

# --- Flask app to keep Render happy ---
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    # Start Reddit loop in background thread
    threading.Thread(target=reddit_loop, daemon=True).start()
    # Run Flask server on Render’s required port
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
