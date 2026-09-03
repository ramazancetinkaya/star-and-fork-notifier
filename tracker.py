#!/usr/bin/env python3
"""
GitHub Public Repositories Star & Fork Activity Tracker.
Monitors user repositories for new stars and forks and dispatches
rich Discord notifications with localized timestamps and rate-limit handling.

Developed by ramazancetinkaya
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("activity-tracker")

STATE_FILE = "last_run.json"
DISCORD_MAX_EMBEDS_PER_MESSAGE = 10


def get_detected_timezone():
    """
    Detects the active timezone of the operating system/runner.
    """
    detected_tz = datetime.now().astimezone().tzinfo
    logger.info("Detected local timezone: %s", detected_tz)
    return detected_tz


LOCAL_TZ = get_detected_timezone()


def load_state() -> Dict[str, Any]:
    """
    Load previous execution state from last_run.json.
    Falls back to one hour prior if absent or invalid.
    """
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "last_run_timestamp" in data:
                    logger.info("Loaded state timestamp: %s", data["last_run_timestamp"])
                    return data
        except (json.JSONDecodeError, OSError) as err:
            logger.warning("Failed to parse %s (%s). Resetting state.", STATE_FILE, err)

    default_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    return {"last_run_timestamp": default_time, "status": "initialized"}


def save_state(status: str, execution_time: datetime, previous_state: Optional[Dict[str, Any]] = None) -> None:
    """
    Persist execution state with both UTC ISO format and localized human-readable time.
    """
    if status == "failed" and previous_state and "last_run_timestamp" in previous_state:
        effective_timestamp = previous_state["last_run_timestamp"]
    else:
        effective_timestamp = execution_time.isoformat()

    local_now = datetime.now(LOCAL_TZ)

    state_data = {
        "last_run_timestamp": effective_timestamp,
        "last_run_local": local_now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }

    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state_data, f, indent=2)
        logger.info("State recorded: status=%s, local_time=%s", status, state_data["last_run_local"])
    except OSError as err:
        logger.error("Error writing %s: %s", STATE_FILE, err)


def get_public_repositories(username: str, headers: Dict[str, str]) -> List[Dict[str, Any]]:
    """
    Retrieve all public repositories owned by the target user.
    """
    repos = []
    page = 1

    while True:
        url = f"https://api.github.com/users/{username}/repos?type=owner&per_page=100&page={page}"
        try:
            res = requests.get(url, headers=headers, timeout=20)
            if res.status_code == 403:
                raise RuntimeError("GitHub API rate limit exceeded.")
            res.raise_for_status()

            batch = res.json()
            if not batch:
                break

            for repo in batch:
                if not repo.get("private", False):
                    repos.append(repo)

            if "next" not in res.links:
                break
            page += 1
        except requests.RequestException as err:
            logger.error("Failed fetching repositories (page %d): %s", page, err)
            raise

    return repos


def get_repo_activity(repo_full_name: str, since_dt: datetime, headers: Dict[str, str]) -> List[Dict[str, Any]]:
    """
    Fetch events for a repository and filter events newer than the cutoff timestamp.
    """
    url = f"https://api.github.com/repos/{repo_full_name}/events?per_page=100"
    try:
        res = requests.get(url, headers=headers, timeout=20)
        if res.status_code in (404, 451):
            return []
        if res.status_code == 403:
            raise RuntimeError("GitHub API rate limit reached.")
        res.raise_for_status()

        events = res.json()
        new_activities = []

        for ev in events:
            if ev.get("type") not in ("WatchEvent", "ForkEvent"):
                continue

            created_at_str = ev.get("created_at")
            if not created_at_str:
                continue

            ev_dt_utc = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))

            if ev_dt_utc <= since_dt:
                break

            new_activities.append(ev)

        return new_activities

    except requests.RequestException as err:
        logger.error("Error checking activity for '%s': %s", repo_full_name, err)
        return []


def build_discord_embed(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a Discord embed utilizing Discord's client-side auto-localized timestamp syntax.
    """
    ev_type = event.get("type")
    actor = event.get("actor", {})
    actor_name = actor.get("login", "Unknown User")
    actor_url = f"https://github.com/{actor_name}"
    actor_avatar = actor.get("avatar_url", "")

    repo_info = event.get("repo", {})
    repo_name = repo_info.get("name", "Unknown Repo")
    repo_url = f"https://github.com/{repo_name}"

    created_at_str = event.get("created_at", datetime.now(timezone.utc).isoformat())
    ev_dt_utc = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
    unix_epoch = int(ev_dt_utc.timestamp())

    # Discord markdown syntax: <t:UNIX:F> renders localized absolute time, <t:UNIX:R> renders relative time
    localized_time_str = f"<t:{unix_epoch}:F> (<t:{unix_epoch}:R>)"

    if ev_type == "WatchEvent":
        color = 16766720  # Gold
        title = "⭐ New Star Received!"
        description = (
            f"**User:** [{actor_name}]({actor_url})\n"
            f"**Repository:** [{repo_name}]({repo_url})\n"
            f"**Time:** {localized_time_str}"
        )
    elif ev_type == "ForkEvent":
        color = 5793266  # Blurple
        title = "🍴 Repository Forked!"
        forkee = event.get("payload", {}).get("forkee", {})
        fork_url = forkee.get("html_url", repo_url)
        fork_name = forkee.get("full_name", "fork")
        description = (
            f"**User:** [{actor_name}]({actor_url})\n"
            f"**Repository:** [{repo_name}]({repo_url})\n"
            f"**Fork:** [{fork_name}]({fork_url})\n"
            f"**Time:** {localized_time_str}"
        )
    else:
        color = 10070709
        title = "GitHub Notification"
        description = (
            f"[{actor_name}]({actor_url}) triggered {ev_type} on [{repo_name}]({repo_url})\n"
            f"**Time:** {localized_time_str}"
        )

    return {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": created_at_str,  # Standard ISO-8601 for footer timestamp
        "author": {
            "name": actor_name,
            "url": actor_url,
            "icon_url": actor_avatar
        },
        "footer": {
            "text": "Developed by ramazancetinkaya"
        }
    }


def send_discord_notifications(webhook_url: str, embeds: List[Dict[str, Any]]) -> None:
    """
    Deliver embeds in batches with rate-limit backoff handling.
    """
    total = len(embeds)
    for i in range(0, total, DISCORD_MAX_EMBEDS_PER_MESSAGE):
        batch = embeds[i:i + DISCORD_MAX_EMBEDS_PER_MESSAGE]
        payload = {
            "username": "GitHub Tracker",
            "avatar_url": "https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png",
            "embeds": batch
        }

        sent = False
        retries = 5
        while not sent and retries > 0:
            try:
                res = requests.post(webhook_url, json=payload, timeout=15)
                if res.status_code == 429:
                    retry_after = res.json().get("retry_after", 2.0)
                    logger.warning("Rate limited by Discord. Backing off for %.2f seconds.", retry_after)
                    time.sleep(float(retry_after) + 0.5)
                    retries -= 1
                    continue

                res.raise_for_status()
                sent = True
                logger.info("Delivered %d notification(s) to Discord.", len(batch))

            except requests.RequestException as err:
                retries -= 1
                logger.error("Failed sending to Discord: %s (%d retries remaining)", err, retries)
                if retries <= 0:
                    raise
                time.sleep(2)

        if i + DISCORD_MAX_EMBEDS_PER_MESSAGE < total:
            time.sleep(1.5)


def main():
    start_time = datetime.now(timezone.utc)
    discord_webhook = os.getenv("DISCORD_WEBHOOK")
    github_token = os.getenv("GITHUB_TOKEN")
    github_user = os.getenv("GITHUB_USERNAME") or os.getenv("GITHUB_REPOSITORY_OWNER")

    if not discord_webhook:
        logger.error("DISCORD_WEBHOOK environment variable is missing.")
        sys.exit(1)

    if not github_user:
        logger.error("GitHub user cannot be identified from environment.")
        sys.exit(1)

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": f"activity-tracker/{github_user}"
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    previous_state = load_state()
    since_dt = datetime.fromisoformat(previous_state["last_run_timestamp"].replace("Z", "+00:00"))

    try:
        repos = get_public_repositories(github_user, headers)
        collected_embeds = []

        for repo in repos:
            repo_name = repo.get("full_name")
            if not repo_name:
                continue

            events = get_repo_activity(repo_name, since_dt, headers)
            for ev in events:
                collected_embeds.append(build_discord_embed(ev))

        logger.info("Found %d new activities across all repositories.", len(collected_embeds))

        if collected_embeds:
            collected_embeds.sort(key=lambda x: x["timestamp"])
            send_discord_notifications(discord_webhook, collected_embeds)
        else:
            logger.info("No new stars or forks found. Webhook omitted.")

        save_state(status="success", execution_time=start_time, previous_state=previous_state)

    except Exception as exc:
        logger.critical("Unexpected execution error: %s", exc, exc_info=True)
        save_state(status="failed", execution_time=start_time, previous_state=previous_state)
        sys.exit(1)


if __name__ == "__main__":
    main()
