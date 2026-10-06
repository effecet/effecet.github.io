#!/usr/bin/env python3
"""Weekly Telegram digest of every open pull request across one GitHub owner.

Lists the owner's public, non-archived repos and sends one HTML message
through the Telegram Bot API with three sections: open PRs with their CI state,
grouped Dependabot / Claude / everyone else; PRs merged in the last 7 days; and
the CI state of each repo's default branch. Standard library only.

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
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

API = "https://api.github.com"
TELEGRAM_LIMIT = 4096
CLAUDE_MARKER = "Generated with [Claude Code]"
MERGED_SHOWN = 8
HEALTH_SHOWN = 10
SEARCH_PAGE = 100  # merged-PR search reads one page; the count says so when it is capped

CIState = Literal["pass", "fail", "cancelled", "pending", "none"]

FAILED = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    title: str
    url: str
    author: str
    body: str
    ci: CIState
    auto_merge: bool


@dataclass(frozen=True)
class Merged:
    repo: str
    number: int
    title: str
    url: str
    author: str
    body: str
    merged_at: str  # ISO 8601, sorts chronologically as a string; "" if unknown (sorts last)


@dataclass(frozen=True)
class Digest:
    open_prs: list[PullRequest]
    merged: list[Merged]
    health: dict[str, CIState]  # repo -> CI state of its default branch
    merged_capped: bool = False  # the search had more results than one page


def group_of(pr: PullRequest | Merged) -> str:
    if pr.author == "dependabot[bot]":
        return "Dependabot"
    if CLAUDE_MARKER in pr.body:
        return "Claude"
    return "Yours"


def ci_state(
    check_runs: list[dict],
    ignore_run_id: str | None = None,
    cancelled_is_failure: bool = True,
) -> CIState:
    """Collapse a commit's check runs into one state. Skipped/neutral count as pass.

    `ignore_run_id` drops check runs belonging to that Actions run, so the
    digest does not report its own still-running job as "CI running".
    With `cancelled_is_failure=False`, a cancelled job (often one that never got
    a runner or hit its timeout) reads as "cancelled" rather than "fail", unless
    something failed; a job still running outranks it.
    """
    if ignore_run_id:
        marker = f"/actions/runs/{ignore_run_id}/"
        check_runs = [r for r in check_runs if marker not in (r.get("details_url") or "")]
    if not check_runs:
        return "none"
    hard = FAILED if cancelled_is_failure else FAILED - {"cancelled"}
    if any(r.get("conclusion") in hard for r in check_runs):
        return "fail"
    if any(r.get("status") != "completed" for r in check_runs):
        return "pending"  # a job still running could yet fail, so it outranks "cancelled"
    if any(r.get("conclusion") == "cancelled" for r in check_runs):
        return "cancelled"
    return "pass"


ICON: dict[CIState, str] = {
    "pass": "✅",
    "fail": "❌",
    "cancelled": "⚠️",
    "pending": "⏳",
    "none": "▫️",
}
GROUP_ICON = {"Dependabot": "🤖", "Claude": "🧠", "Yours": "👤"}
GROUP_COUNT_LABEL = {"Dependabot": "Dependabot", "Claude": "Claude", "Yours": "yours"}
SEVERITY: list[CIState] = ["fail", "cancelled", "pending", "none", "pass"]
HEALTH_NOTE: dict[CIState, str] = {
    "fail": "CI failing",
    "cancelled": "CI cancelled",
    "pending": "CI running",
    "none": "no CI checks",
}


def merged_query(owner: str, now: datetime) -> str:
    """Search query for PRs merged in the 7 days up to `now` (UTC, to the second)."""
    since = (now - timedelta(days=7)).astimezone(UTC)
    return f"is:pr is:merged user:{owner} merged:>={since:%Y-%m-%dT%H:%M:%SZ}"


def parse_merged(search: dict, repos: set[str]) -> list[Merged]:
    """Search-API items -> Merged, keeping only repos the digest covers."""
    merged = []
    for item in search.get("items", []):
        repo = item["repository_url"].rsplit("/", 1)[-1]
        if repo not in repos:
            continue
        merged.append(
            Merged(
                repo=repo,
                number=item["number"],
                title=item["title"],
                url=item["html_url"],
                author=item["user"]["login"],
                body=item.get("body") or "",
                merged_at=(item.get("pull_request") or {}).get("merged_at") or "",
            )
        )
    return merged


def units(text: str) -> int:
    """Length as Telegram counts it (UTF-16 code units). Counting the raw HTML,
    tags included, over-estimates what Telegram measures, so it is a safe bound."""
    return len(text.encode("utf-16-le")) // 2


def _link(item: PullRequest | Merged) -> str:
    return f'<a href="{html.escape(item.url)}">{html.escape(item.repo)}#{item.number}</a>'


def _open_section(prs: list[PullRequest], budget: int) -> list[str]:
    """Open-PR lines, cut to fit `budget` chars so the sections after it always fit."""
    if not prs:
        return ["📬 <b>Open PRs</b>: none, inbox zero ✨"]
    failing = sum(pr.ci == "fail" for pr in prs)
    queued = sum(pr.auto_merge for pr in prs)
    lines = [
        "📬 <b>Open PRs</b>",
        f"{len(prs)} open · {failing} failing CI · {queued} queued to auto-merge",
    ]
    for group in ("Dependabot", "Claude", "Yours"):
        members = sorted(
            (pr for pr in prs if group_of(pr) == group), key=lambda p: (p.repo, p.number)
        )
        if not members:
            continue
        lines.append(f"{GROUP_ICON[group]} <b>{group}</b> ({len(members)})")
        for pr in members:
            tag = " · auto-merge queued" if pr.auto_merge else ""
            lines.append(f"   {ICON[pr.ci]} {_link(pr)} {html.escape(pr.title[:90])}{tag}")

    more = "   … more open PRs: see the link below"
    out: list[str] = []
    for line in lines:
        if units("\n".join([*out, line, more])) > budget:
            if units("\n".join([*out, more])) <= budget:
                out.append(more)
            break
        out.append(line)
    return out


def _merged_section(merged: list[Merged], capped: bool) -> list[str]:
    if not merged:
        return ["🔀 <b>Merged</b>: none"]
    counts = {g: sum(group_of(m) == g for m in merged) for g in GROUP_ICON}
    total = f"{len(merged)} (search capped at {SEARCH_PAGE})" if capped else str(len(merged))
    split = " · ".join(f"{GROUP_ICON[g]} {counts[g]} {GROUP_COUNT_LABEL[g]}" for g in GROUP_ICON)
    lines = [f"🔀 <b>Merged</b>: {total}", f"   {split}"]
    for m in sorted(merged, key=lambda m: m.merged_at, reverse=True)[:MERGED_SHOWN]:
        lines.append(f"   • {_link(m)} {html.escape(m.title[:70])}")
    if len(merged) > MERGED_SHOWN:
        lines.append(f"   +{len(merged) - MERGED_SHOWN} more")
    return lines


def _health_section(health: dict[str, CIState]) -> list[str]:
    green = sum(state == "pass" for state in health.values())
    done = " ✅" if green == len(health) else ""
    lines = [f"🩺 <b>Default-branch CI</b>: {green}/{len(health)} repos green{done}"]
    # Worst first, so a real failure never ends up behind "+N more".
    not_green = sorted(
        (repo for repo in health if health[repo] != "pass"),
        key=lambda repo: (SEVERITY.index(health[repo]), repo),
    )
    for repo in not_green[:HEALTH_SHOWN]:
        state = health[repo]
        lines.append(f"   {ICON[state]} {html.escape(repo)}: {HEALTH_NOTE[state]}")
    if len(not_green) > HEALTH_SHOWN:
        lines.append(f"   +{len(not_green) - HEALTH_SHOWN} more")
    return lines


def render(digest: Digest, owner: str, now: datetime) -> str:
    """Telegram-HTML message, under Telegram's 4096-char limit.

    Only the open-PR list is ever cut; the merged and CI sections are short and
    bounded, so they always make it into the message.
    """
    head = [
        f"🗞 <b>Weekly PR digest</b> · {html.escape(owner)}",
        f"📅 {now:%a} {now.day} {now:%b %Y} · last 7 days",
        "",
    ]
    tail = [
        "",
        *_merged_section(digest.merged, digest.merged_capped),
        "",
        *_health_section(digest.health),
    ]
    pulls = f"https://github.com/pulls?q=is%3Aopen+user%3A{owner}"
    footer = f'\n\n🔗 <a href="{html.escape(pulls)}">All open PRs on GitHub</a>'
    fixed = units("\n".join(head)) + units("\n".join(tail)) + units(footer) + 2
    middle = _open_section(digest.open_prs, TELEGRAM_LIMIT - fixed)
    return "\n".join([*head, *middle, *tail]) + footer


def _get(path: str, token: str) -> Any:
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


def _ci_of(
    owner: str, repo: str, ref: str, token: str, cancelled_is_failure: bool = True
) -> CIState:
    """CI state of a commit or branch. An empty repo (404/409) reads as "none".

    Any other API error (bad token, rate limit) is raised, so a digest that
    could not read CI fails loudly instead of reporting "no CI checks".
    """
    ref = urllib.parse.quote(ref, safe="")
    try:
        runs = _get(f"/repos/{owner}/{repo}/commits/{ref}/check-runs?per_page=100", token)
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 409):
            return "none"
        raise
    return ci_state(
        runs.get("check_runs", []),
        os.environ.get("GITHUB_RUN_ID"),
        cancelled_is_failure=cancelled_is_failure,
    )


def collect(owner: str, token: str, now: datetime) -> Digest:
    repos = _get(f"/users/{owner}/repos?per_page=100&type=owner", token)
    prs: list[PullRequest] = []
    health: dict[str, CIState] = {}
    for repo in repos:
        if repo.get("archived") or repo.get("private") or repo.get("fork"):
            continue
        name = repo["name"]
        health[name] = _ci_of(
            owner, name, repo["default_branch"], token, cancelled_is_failure=False
        )
        for p in _get(f"/repos/{owner}/{name}/pulls?state=open&per_page=100", token):
            prs.append(
                PullRequest(
                    repo=name,
                    number=p["number"],
                    title=p["title"],
                    url=p["html_url"],
                    author=p["user"]["login"],
                    body=p.get("body") or "",
                    ci=_ci_of(owner, name, p["head"]["sha"], token),
                    auto_merge=p.get("auto_merge") is not None,
                )
            )

    query = urllib.parse.quote(merged_query(owner, now))
    found = _get(f"/search/issues?q={query}&per_page={SEARCH_PAGE}", token)
    return Digest(
        open_prs=prs,
        merged=parse_merged(found, set(health)),
        health=health,
        merged_capped=found.get("total_count", 0) > SEARCH_PAGE,
    )


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

    now = datetime.now(UTC)
    text = render(collect(owner, gh_token, now), owner, now)
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
