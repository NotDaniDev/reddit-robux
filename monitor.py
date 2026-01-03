import time
import requests
import praw

# --- Reddit setup ---
reddit = praw.Reddit(
    client_id="wZsGSUwlJ2dM5jXC4om6TA",
    client_secret="zhwFJO7Lz-TP1o_sbSD6QVmeZfrKyg",
    user_agent="GiveawayNotifierBot"
)

subreddit = reddit.subreddit("plsdonategame")
TARGET_FLAIRS = {"Free Giveaway", "Requirement Giveaway"}

# --- Discord webhook ---
WEBHOOK_URL = "https://discord.com/api/webhooks/974312334795350116/RM5_F1wbexL8aOaBZfileGWP3n4UMeQXHklFrPgRMru4dOEkklLLdRXFOb_gNy-Tnhy7"

seen_posts = set()

def send_to_discord(submission):
    post_url = f"https://reddit.com{submission.permalink}"  # always the Reddit post link
    data = {
        "content": f"<@775546382416871425> 🎉 New giveaway!\n**{submission.title}**\n{post_url}"
    }
    r = requests.post(WEBHOOK_URL, json=data)
    print("Discord response:", r.status_code)

def main():
    while True:
        for submission in subreddit.new(limit=5):
            if submission.id not in seen_posts and submission.link_flair_text in TARGET_FLAIRS:
                seen_posts.add(submission.id)
                send_to_discord(submission)
        time.sleep(30)  # check every 30 seconds

if __name__ == "__main__":
    main()
