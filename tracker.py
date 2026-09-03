"""
GitHub Public Repository Activity Tracker (Stars & Forks)
Sends real-time embed notifications to Discord via Webhooks.

Author: Developed by ramazancetinkaya
"""

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- Configuration & Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("tracker")

STATE_FILE = "last_run.json"
MAX_EMBEDS_PER_DISCORD_MSG = 10
DISCORD_RATE_LIMIT_DELAY = 1.5  # Seconds between batch dispatches
DEV_FOOTER = "Developed by ramazancetinkaya"


@dataclass
class ActivityEvent:
    event_id: str
    event_type: str  # 'star' or 'fork'
    actor_login: str
    actor_url: str
    actor_avatar: str
    repo_name: str
    repo_url: str
    created_at: datetime


def get_http_session() -> requests.Session:
    """Configures and returns a requests.Session with connection retries."""
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def load_last_run(filepath: str) -> Optional[datetime]:
    """Loads the last execution timestamp from JSON. Returns None if uninitialized."""
    if not os.path.exists(filepath):
        logger.info(f"State file '{filepath}' not found. Performing initial baseline setup.")
        return None

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            iso_str = data.get("last_checked_at")
            if iso_str:
                return datetime.fromisoformat(iso_str)
    except Exception as exc:
        logger.warning(f"Failed to parse '{filepath}': {exc}. Starting fresh baseline.")
    return None


def save_last_run(filepath: str, timestamp: datetime) -> None:
    """Saves the current execution timestamp to JSON."""
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump({"last_checked_at": timestamp.isoformat()}, f, indent=2)
        logger.info(f"Updated state in '{filepath}' to {timestamp.isoformat()}.")
    except Exception as exc:
        logger.error(f"Critical error saving state to '{filepath}': {exc}")
        sys.exit(1)


def get_public_repositories(
    session: requests.Session, username: str, token: Optional[str]
) -> List[Dict[str, Any]]:
    """Fetches all public repositories owned by the specified user."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    repos: List[Dict[str, Any]] = []
    page = 1

    while True:
        url = f"https://api.github.com/users/{username}/repos"
        params = {"type": "public", "per_page": 100, "page": page, "sort": "updated"}
        try:
            resp = session.get(url, headers=headers, params=params, timeout=20)
            if resp.status_code == 404:
                logger.error(f"GitHub user '{username}' does not exist.")
                return []
            resp.raise_for_status()

            batch = resp.json()
            if not batch:
                break

            # Keep only source repos directly owned by the target user
            for repo in batch:
                if not repo.get("fork", False) and repo.get("owner", {}).get("login", "").lower() == username.lower():
                    repos.append(repo)

            if len(batch) < 100:
                break
            page += 1
        except requests.RequestException as exc:
            logger.error(f"Error fetching public repos for '{username}': {exc}")
            break

    logger.info(f"Retrieved {len(repos)} active public repositories.")
    return repos


def fetch_repo_events(
    session: requests.Session,
    repo_full_name: str,
    since: datetime,
    token: Optional[str],
) -> List[ActivityEvent]:
    """Inspects recent repository events for stars (WatchEvent) and forks (ForkEvent)."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"https://api.github.com/repos/{repo_full_name}/events"
    events: List[ActivityEvent] = []

    try:
        resp = session.get(url, headers=headers, params={"per_page": 30}, timeout=15)
        if resp.status_code in (404, 403):
            # 403 might indicate secondary rate limits or restricted permissions
            return []
        resp.raise_for_status()

        for raw_event in resp.json():
            event_time_str = raw_event.get("created_at")
            if not event_time_str:
                continue

            event_time = datetime.fromisoformat(event_time_str.replace("Z", "+00:00"))
            if event_time <= since:
                continue

            event_type = raw_event.get("type")
            actor = raw_event.get("actor", {})
            actor_login = actor.get("login", "unknown")
            actor_url = f"https://github.com/{actor_login}"
            actor_avatar = actor.get("avatar_url", "")
            repo_name = raw_event.get("repo", {}).get("name", repo_full_name)
            repo_url = f"https://github.com/{repo_name}"

            # Star event check
            if event_type == "WatchEvent" and raw_event.get("payload", {}).get("action") == "started":
                events.append(
                    ActivityEvent(
                        event_id=raw_event["id"],
                        event_type="star",
                        actor_login=actor_login,
                        actor_url=actor_url,
                        actor_avatar=actor_avatar,
                        repo_name=repo_name,
                        repo_url=repo_url,
                        created_at=event_time,
                    )
                )

            # Fork event check
            elif event_type == "ForkEvent":
                events.append(
                    ActivityEvent(
                        event_id=raw_event["id"],
                        event_type="fork",
                        actor_login=actor_login,
                        actor_url=actor_url,
                        actor_avatar=actor_avatar,
                        repo_name=repo_name,
                        repo_url=repo_url,
                        created_at=event_time,
                    )
                )

    except requests.RequestException as exc:
        logger.warning(f"Could not read events for '{repo_full_name}': {exc}")

    return events


def build_discord_embed(event: ActivityEvent) -> Dict[str, Any]:
    """Generates a Discord embed payload according to event type."""
    if event.event_type == "star":
        title = "⭐ New Star Received!"
        color = 0xF1C40F  # Gold
        action_text = "starred"
    else:
        title = "🍴 New Fork Created!"
        color = 0x3498DB  # Blue
        action_text = "forked"

    description = (
        f"👤 **[{event.actor_login}]({event.actor_url})** has {action_text} "
        f"📁 **[{event.repo_name}]({event.repo_url})**"
    )

    embed: Dict[str, Any] = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": event.created_at.isoformat(),
        "footer": {"text": DEV_FOOTER},
    }

    if event.actor_avatar:
        embed["thumbnail"] = {"url": event.actor_avatar}

    return embed


def dispatch_to_discord(
    session: requests.Session, webhook_url: str, events: List[ActivityEvent]
) -> None:
    """Dispatches embed batches to Discord, observing payload caps and rate limits."""
    if not events:
        logger.info("No events to send.")
        return

    # Sort events chronologically
    events.sort(key=lambda e: e.created_at)
    embeds = [build_discord_embed(ev) for ev in events]

    # Chunk into batches of up to 10 embeds per payload
    for i in range(0, len(embeds), MAX_EMBEDS_PER_DISCORD_MSG):
        chunk = embeds[i : i + MAX_EMBEDS_PER_DISCORD_MSG]
        payload = {"embeds": chunk}

        dispatched = False
        max_retries = 3

        while not dispatched and max_retries > 0:
            try:
                resp = session.post(webhook_url, json=payload, timeout=15)

                if resp.status_code == 429:
                    rate_data = resp.json()
                    retry_after = rate_data.get("retry_after", 2.0)
                    logger.warning(f"Discord 429 encountered. Backing off for {retry_after}s.")
                    time.sleep(float(retry_after) + 0.5)
                    max_retries -= 1
                    continue

                resp.raise_for_status()
                dispatched = True
                logger.info(f"Dispatched batch of {len(chunk)} events to Discord.")
            except requests.RequestException as exc:
                logger.error(f"Error sending payload to Discord: {exc}")
                max_retries -= 1
                time.sleep(2.0)

        # Respect Discord webhook channel throughput
        time.sleep(DISCORD_RATE_LIMIT_DELAY)


def main() -> None:
    webhook_url = os.getenv("DISCORD_WEBHOOK")
    github_token = os.getenv("GITHUB_TOKEN")
    github_user = os.getenv("GITHUB_REPOSITORY_OWNER") or os.getenv("GITHUB_ACTOR")

    if not webhook_url:
        logger.error("Missing DISCORD_WEBHOOK environment variable.")
        sys.exit(1)

    if not github_user:
        logger.error("Could not determine GitHub username from environment.")
        sys.exit(1)

    session = get_http_session()
    current_run_time = datetime.now(timezone.utc)
    last_run_time = load_last_run(STATE_FILE)

    # First-time initialization safeguard: avoid historical mass-notification
    if last_run_time is None:
        save_last_run(STATE_FILE, current_run_time)
        logger.info("Initialization complete. Tracking starts from next cycle.")
        return

    logger.info(f"Checking events since {last_run_time.isoformat()} for user '{github_user}'.")
    repos = get_public_repositories(session, github_user, github_token)

    all_events: List[ActivityEvent] = []
    seen_ids = set()

    for repo in repos:
        repo_name = repo["full_name"]
        events = fetch_repo_events(session, repo_name, last_run_time, github_token)
        for ev in events:
            if ev.event_id not in seen_ids:
                seen_ids.add(ev.event_id)
                all_events.append(ev)

    if all_events:
        logger.info(f"Discovered {len(all_events)} new star/fork event(s). Sending notifications...")
        dispatch_to_discord(session, webhook_url, all_events)
    else:
        logger.info("No new stars or forks detected during this interval.")

    # Update state only after successful pass
    save_last_run(STATE_FILE, current_run_time)


if __name__ == "__main__":
    main()
