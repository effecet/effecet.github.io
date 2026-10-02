import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pr_digest as d


def make(**kw):
    base = {
        "repo": "repo",
        "number": 1,
        "title": "t",
        "url": "https://github.com/o/repo/pull/1",
        "author": "effecet",
        "body": "",
        "ci": "pass",
        "auto_merge": False,
    }
    base.update(kw)
    return d.PullRequest(**base)


def test_ci_state_failure_wins_over_pending():
    runs = [
        {"status": "in_progress", "conclusion": None},
        {"status": "completed", "conclusion": "failure"},
    ]
    assert d.ci_state(runs) == "fail"


def test_ci_state_cancelled_counts_as_failure():
    assert d.ci_state([{"status": "completed", "conclusion": "cancelled"}]) == "fail"


def test_ci_state_skipped_and_neutral_are_pass():
    runs = [
        {"status": "completed", "conclusion": "skipped"},
        {"status": "completed", "conclusion": "neutral"},
    ]
    assert d.ci_state(runs) == "pass"


def test_ci_state_pending_and_none():
    assert d.ci_state([{"status": "queued", "conclusion": None}]) == "pending"
    assert d.ci_state([]) == "none"


def test_group_of_dependabot_claude_and_human():
    assert d.group_of(make(author="dependabot[bot]")) == "Dependabot"
    assert (
        d.group_of(make(body="x\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"))
        == "Claude"
    )
    assert d.group_of(make()) == "Yours"


def test_render_empty_says_nothing_open():
    assert "Nothing open" in d.render([], "effecet")


def test_render_counts_groups_and_escapes_html():
    prs = [
        make(
            repo="a",
            number=2,
            author="dependabot[bot]",
            ci="fail",
            auto_merge=True,
            title="bump <x> & y",
        ),
        make(repo="b", number=5, body=d.CLAUDE_MARKER),
    ]
    out = d.render(prs, "effecet")
    assert "2 open · 1 failing CI · 1 queued to auto-merge" in out
    assert "<b>Dependabot</b> (1)" in out and "<b>Claude</b> (1)" in out
    assert "<b>Yours</b>" not in out
    assert "bump &lt;x&gt; &amp; y" in out
    assert "auto-merge queued" in out


def test_render_stays_under_telegram_limit():
    prs = [make(repo=f"repo{i}", number=i, title="x" * 90) for i in range(200)]
    out = d.render(prs, "effecet")
    assert len(out) <= d.TELEGRAM_LIMIT
    assert "and more" in out
