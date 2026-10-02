#!/usr/bin/env python3
"""Weekly Telegram digest of every open pull request across one GitHub owner.

Lists the owner's public, non-archived repos, collects open PRs with their CI
state, groups them (Dependabot / Claude / everyone else) and sends one HTML
message through the Telegram Bot API. Standard library only.

Environment:
    GITHUB_TOKEN        token for the GitHub REST API (the workflow's own is enough)
    DIGEST_OWNER        account to scan (default: effecet)
    TELEGRAM_BOT_TOKEN  bot token; if unset, the digest is printed, not sent
    TELEGRAM_CHAT_ID    chat to send to
    GITHUB_STEP_SUMMARY when set (Actions), the digest is also written there
"""

from __future__ import annotations

import html
import json
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass

API = "https://api.github.com"
TELEGRAM_LIMIT = 4096
CLAUDE_MARKER = "Generated with [Claude Code]"

FAILED = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    title: str
    url: str
    author: str
    body: str
    ci: str  # "pass" | "fail" | "pending" | "none"
    auto_merge: bool


def group_of(pr: PullRequest) -> str:
    if pr.author == "dependabot[bot]":
        return "Dependabot"
    if CLAUDE_MARKER in pr.body:
        return "Claude"
    return "Yours"


def ci_state(check_runs: list[dict]) -> str:
    """Collapse a commit's check runs into one state. Skipped/neutral count as pass."""
    if not check_runs:
        return "none"
    if any(r.get("conclusion") in FAILED for r in check_runs):
        return "fail"
    if any(r.get("status") != "completed" for r in check_runs):
        return "pending"
    return "pass"


ICON = {"pass": "✅", "fail": "❌", "pending": "⏳", "none": "▫️"}


def render(prs: list[PullRequest], owner: str) -> str:
    """Telegram-HTML message. Stays under Telegram's 4096-char limit."""
    if not prs:
        return f"<b>Weekly PR digest · {html.escape(owner)}</b>\nNothing open ✅"

    failing = sum(pr.ci == "fail" for pr in prs)
    queued = sum(pr.auto_merge for pr in prs)
    lines = [
        f"<b>Weekly PR digest · {html.escape(owner)}</b>",
        f"{len(prs)} open · {failing} failing CI · {queued} queued to auto-merge",
    ]
    for group in ("Dependabot", "Claude", "Yours"):
        members = sorted(
            (pr for pr in prs if group_of(pr) == group), key=lambda p: (p.repo, p.number)
        )
        if not members:
            continue
        lines.append(f"\n<b>{group}</b> ({len(members)})")
        for pr in members:
            tag = " · auto-merge queued" if pr.auto_merge else ""
            title = html.escape(pr.title[:90])
            lines.append(
                f'{ICON[pr.ci]} <a href="{html.escape(pr.url)}">{html.escape(pr.repo)}#{pr.number}</a> '
                f"{title}{tag}"
            )

    more = f"… and more: https://github.com/pulls?q=is%3Aopen+user%3A{owner}"
    out: list[str] = []
    for line in lines:
        # Leave room for the "and more" line itself, so the cut never overflows.
        if len("\n".join([*out, line])) + len(more) + 1 > TELEGRAM_LIMIT:
            out.append(more)
            break
        out.append(line)
    return "\n".join(out)


def _get(path: str, token: str) -> list | dict:
    req = urllib.request.Request(
        f"{API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def collect(owner: str, token: str) -> list[PullRequest]:
    repos = _get(f"/users/{owner}/repos?per_page=100&type=owner", token)
    prs: list[PullRequest] = []
    for repo in repos:
        if repo.get("archived") or repo.get("private") or repo.get("fork"):
            continue
        name = repo["name"]
        for p in _get(f"/repos/{owner}/{name}/pulls?state=open&per_page=100", token):
            runs = _get(
                f"/repos/{owner}/{name}/commits/{p['head']['sha']}/check-runs?per_page=100", token
            )
            prs.append(
                PullRequest(
                    repo=name,
                    number=p["number"],
                    title=p["title"],
                    url=p["html_url"],
                    author=p["user"]["login"],
                    body=p.get("body") or "",
                    ci=ci_state(runs.get("check_runs", [])),
                    auto_merge=p.get("auto_merge") is not None,
                )
            )
    return prs


def send(text: str, bot_token: str, chat_id: str) -> None:
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage", data=data, method="POST"
    )
    # Never let the URL (it embeds the bot token) reach a traceback or log line.
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ok = json.load(resp).get("ok")
    except Exception as exc:  # noqa: BLE001 - report the type only, not the URL
        raise SystemExit(f"Telegram send failed: {type(exc).__name__}") from None
    if not ok:
        raise SystemExit("Telegram send failed: API returned ok=false")


def main() -> int:
    owner = os.environ.get("DIGEST_OWNER", "effecet")
    gh_token = os.environ.get("GITHUB_TOKEN")
    if not gh_token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 2

    text = render(collect(owner, gh_token), owner)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text.replace("\n", "  \n") + "\n")

    bot, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (bot and chat):
        print("::notice::TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; digest printed only")
        return 0
    send(text, bot, chat)
    print("sent to Telegram")
    return 0


if __name__ == "__main__":
    sys.exit(main())
