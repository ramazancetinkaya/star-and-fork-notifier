import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta
import urllib.request
import urllib.error

# Environment variables provided by GitHub Actions
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
GITHUB_ACTOR = os.environ.get("GITHUB_REPOSITORY_OWNER") or os.environ.get("GITHUB_ACTOR")
EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "")

STATE_FILE = "last_run.json"
MIN_INTERVAL_MINUTES = 25  # Skip scheduled check if manual run happened recently

def get_headers():
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "star-and-fork-tracker"
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    return headers

def make_request(url):
    req = urllib.request.Request(url, headers=get_headers())
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code} for {url}: {e.reason}")
        return None
    except Exception as e:
        print(f"Network error fetching {url}: {e}")
        return None

def get_public_repos(username):
    repos = []
    page = 1
    # Handle pagination for users with more than 100 repositories
    while True:
        url = f"https://api.github.com/users/{username}/repos?type=public&per_page=100&page={page}"
        data = make_request(url)
        if not data or not isinstance(data, list):
            break
        repos.extend(data)
        if len(data) < 100:
            break
        page += 1
    return repos

def send_discord_notification(content):
    if not DISCORD_WEBHOOK:
        print("Error: DISCORD_WEBHOOK is not set.")
        return
    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "GitHub-Actions"}
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        time.sleep(1)  # Safeguard against Discord webhook rate limiting
    except Exception as e:
        print(f"Failed to deliver Discord notification: {e}")

def main():
    now = datetime.now(timezone.utc)

    # 1. State Management: Read last check timestamp
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
                last_run = datetime.fromisoformat(state.get("last_run"))
        except Exception as e:
            print(f"Could not read state file, defaulting to 30 mins ago: {e}")
            last_run = now - timedelta(minutes=30)
    else:
        # First run: only inspect the last 30 minutes to prevent spamming old events
        last_run = now - timedelta(minutes=30)

    # 2. Reset Interval Check: If scheduled, make sure 25+ minutes elapsed since last check
    time_diff = now - last_run
    if EVENT_NAME == "schedule" and time_diff < timedelta(minutes=MIN_INTERVAL_MINUTES):
        print(f"Skipping: Last run occurred {time_diff.total_seconds() / 60:.1f} mins ago (Threshold: {MIN_INTERVAL_MINUTES} mins).")
        sys.exit(0)

    print(f"Checking events between {last_run.isoformat()} and {now.isoformat()} for user: {GITHUB_ACTOR}")

    repos = get_public_repos(GITHUB_ACTOR)
    print(f"Found {len(repos)} public repositories.")

    alerts = []

    # 3. Fetch recent events per repository
    for repo in repos:
        repo_name = repo["name"]
        events_url = f"https://api.github.com/repos/{GITHUB_ACTOR}/{repo_name}/events?per_page=30"
        events = make_request(events_url)

        if not events or not isinstance(events, list):
            continue

        for ev in events:
            ev_time_str = ev.get("created_at")
            if not ev_time_str:
                continue
            ev_time = datetime.fromisoformat(ev_time_str.replace("Z", "+00:00"))

            # Events are ordered newest first; stop evaluating if event is older than last_run
            if ev_time <= last_run:
                break

            actor = ev.get("actor", {}).get("login", "Unknown")
            repo_full = ev.get("repo", {}).get("name", f"{GITHUB_ACTOR}/{repo_name}")

            # Match Star events
            if ev.get("type") == "WatchEvent" and ev.get("payload", {}).get("action") == "started":
                alerts.append(f"⭐ **{actor}** just starred [{repo_full}](https://github.com/{repo_full})")
            
            # Match Fork events
            elif ev.get("type") == "ForkEvent":
                forkee_url = ev.get("payload", {}).get("forkee", {}).get("html_url", f"https://github.com/{repo_full}")
                alerts.append(f"🍴 **{actor}** just forked [{repo_full}](https://github.com/{repo_full}) ➔ [View Fork]({forkee_url})")

    # 4. Dispatch Discord Alerts (Chronological order)
    alerts.reverse()
    for alert in alerts:
        send_discord_notification(alert)

    # 5. Persist the current timestamp for subsequent runs
    with open(STATE_FILE, "w") as f:
        json.dump({"last_run": now.isoformat()}, f, indent=2)

    print(f"Processed {len(alerts)} alerts. State updated successfully.")

if __name__ == "__main__":
    main()
