import sys
import typing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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


def merged(**kw):
    base = {
        "repo": "repo",
        "number": 1,
        "title": "t",
        "url": "https://github.com/o/repo/pull/1",
        "author": "effecet",
        "body": "",
        "merged_at": "2026-10-01T00:00:00Z",
    }
    base.update(kw)
    return d.Merged(**base)


NOW = datetime(2026, 10, 5, 17, 23, tzinfo=UTC)


def digest(open_prs=(), merged_prs=(), health=None, capped=False):
    return d.Digest(
        open_prs=list(open_prs),
        merged=list(merged_prs),
        health=health if health is not None else {"a": "pass", "b": "pass"},
        merged_capped=capped,
    )


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


def test_ci_state_ignores_the_digest_own_run():
    own = {
        "status": "in_progress",
        "conclusion": None,
        "details_url": "https://github.com/effecet/effecet.github.io/actions/runs/42/job/7",
    }
    other = {
        "status": "completed",
        "conclusion": "success",
        "details_url": "https://github.com/effecet/effecet.github.io/actions/runs/420/job/1",
    }
    assert d.ci_state([own, other], ignore_run_id="42") == "pass"
    assert d.ci_state([own, other]) == "pending"
    assert d.ci_state([own], ignore_run_id="42") == "none"


def test_group_of_dependabot_claude_and_human():
    assert d.group_of(make(author="dependabot[bot]")) == "Dependabot"
    assert (
        d.group_of(make(body="x\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"))
        == "Claude"
    )
    assert d.group_of(make()) == "Yours"


FOOTER = "All open PRs on GitHub</a>"


def test_render_quiet_week_still_reports_merges_and_health():
    out = d.render(digest(), "effecet", NOW)
    assert "Weekly PR digest" in out and "effecet" in out
    assert "Mon 5 Oct 2026 · last 7 days" in out
    assert "inbox zero" in out
    assert "Merged</b>: none" in out
    assert "2/2 repos green" in out
    assert out.endswith(FOOTER)


def test_render_open_prs_counts_groups_and_escapes_html():
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
    out = d.render(digest(open_prs=prs), "effecet", NOW)
    assert "2 open · 1 failing CI · 1 queued to auto-merge" in out
    assert "Dependabot</b> (1)" in out and "Claude</b> (1)" in out
    assert "Yours</b>" not in out
    assert "bump &lt;x&gt; &amp; y" in out
    assert "auto-merge queued" in out
    assert "inbox zero" not in out


def test_render_merged_counts_by_group():
    prs = [merged(repo="dep", number=i, author="dependabot[bot]") for i in range(3)]
    prs.append(merged(repo="cl", number=99, body=d.CLAUDE_MARKER))
    out = d.render(digest(merged_prs=prs), "effecet", NOW)
    assert "Merged</b>: 4\n" in out
    assert "🤖 3 Dependabot · 🧠 1 Claude · 👤 0 yours" in out


def test_render_merged_cap_keeps_the_newest():
    start = datetime(2026, 9, 29, tzinfo=UTC)
    prs = [
        merged(repo=f"r{i}", merged_at=(start + timedelta(hours=i)).isoformat())
        for i in range(d.MERGED_SHOWN + 2)
    ]
    out = d.render(digest(merged_prs=prs), "effecet", NOW)
    newest, oldest = f"r{d.MERGED_SHOWN + 1}#1", "r0#1"
    assert out.count("• ") == d.MERGED_SHOWN
    assert newest in out and oldest not in out and "r1#1" not in out
    assert out.index(newest) < out.index(f"r{d.MERGED_SHOWN}#1")
    assert "+2 more" in out


def test_render_merged_exactly_at_the_cap_has_no_more_line():
    prs = [merged(repo=f"r{i}") for i in range(d.MERGED_SHOWN)]
    out = d.render(digest(merged_prs=prs), "effecet", NOW)
    assert out.count("• ") == d.MERGED_SHOWN
    assert "more\n" not in out and "+0" not in out


def test_render_merged_count_marks_a_capped_search():
    out = d.render(digest(merged_prs=[merged()], capped=True), "effecet", NOW)
    assert f"Merged</b>: 1 (search capped at {d.SEARCH_PAGE})" in out


def test_render_merged_escapes_titles():
    out = d.render(digest(merged_prs=[merged(title="a <b> & c")]), "effecet", NOW)
    assert "a &lt;b&gt; &amp; c" in out


def test_render_health_lists_only_repos_that_are_not_green():
    health = {"ok": "pass", "broken": "fail", "busy": "pending", "bare": "none"}
    out = d.render(digest(health=health), "effecet", NOW)
    assert "1/4 repos green" in out
    assert "❌ broken: CI failing" in out
    assert "⏳ busy: CI running" in out and "▫️ bare: no CI checks" in out
    assert "ok:" not in out


def test_render_health_all_green_lists_no_repos():
    out = d.render(digest(health={"a": "pass"}), "effecet", NOW)
    assert "1/1 repos green ✅" in out
    assert "a:" not in out


def test_merged_query_covers_exactly_the_last_seven_days():
    q = d.merged_query("effecet", NOW)
    assert q == "is:pr is:merged user:effecet merged:>=2026-09-28T17:23:00Z"


def item(repo, n, **extra):
    base = {
        "repository_url": f"https://api.github.com/repos/effecet/{repo}",
        "number": n,
        "title": "t",
        "html_url": f"https://github.com/effecet/{repo}/pull/{n}",
        "user": {"login": "dependabot[bot]"},
        "body": None,
        "pull_request": {"merged_at": "2026-10-02T03:00:00Z"},
    }
    base.update(extra)
    return base


def test_parse_merged_keeps_only_covered_repos():
    out = d.parse_merged({"items": [item("kept", 1), item("archived", 2)]}, {"kept"})
    assert [(m.repo, m.number, m.body, m.merged_at) for m in out] == [
        ("kept", 1, "", "2026-10-02T03:00:00Z")
    ]
    assert d.group_of(out[0]) == "Dependabot"


def test_parse_merged_tolerates_a_missing_pull_request_field():
    found = {"items": [item("kept", 1, pull_request=None), item("kept", 2)]}
    found["items"][1].pop("pull_request")
    assert [m.merged_at for m in d.parse_merged(found, {"kept"})] == ["", ""]


def test_render_long_backlog_keeps_every_section_and_the_limit():
    health = {"good": "pass", "bad": "fail"}
    for title_len in (1, 20, 45, 70, 90):
        prs = [make(repo=f"repo{i}", number=i, title="x" * title_len) for i in range(300)]
        out = d.render(digest(open_prs=prs, merged_prs=[merged()], health=health), "effecet", NOW)
        assert d.units(out) <= d.TELEGRAM_LIMIT, title_len
        assert "more open PRs" in out
        assert "Merged</b>: 1" in out
        assert "❌ bad: CI failing" in out
        assert out.endswith(FOOTER)


def test_open_section_stays_within_its_budget():
    prs = [make(repo=f"repo{i}", number=i, title="x" * 40) for i in range(100)]
    for budget in (10, 50, 300, 1000, 2500):
        assert d.units("\n".join(d._open_section(prs, budget))) <= budget


def test_units_counts_emoji_outside_the_bmp_as_two():
    assert d.units("a✅🗞") == 1 + 1 + 2


def test_render_health_exactly_at_the_cap_has_no_more_line():
    health = {f"r{i:02}": "fail" for i in range(d.HEALTH_SHOWN)}
    out = d.render(digest(health=health), "effecet", NOW)
    assert out.count("CI failing") == d.HEALTH_SHOWN
    assert "more" not in out.split("Default-branch CI")[1]


def test_render_health_caps_the_failing_list():
    health = {f"r{i:02}": "fail" for i in range(d.HEALTH_SHOWN + 3)}
    out = d.render(digest(health=health), "effecet", NOW)
    assert out.count("CI failing") == d.HEALTH_SHOWN
    assert "+3 more" in out


def test_ci_of_reads_an_api_error_as_no_ci_and_quotes_the_ref(monkeypatch):
    seen = []

    def empty_repo(path, token):
        seen.append(path)
        raise d.urllib.error.HTTPError(path, 409, "Git Repository is empty", {}, None)

    monkeypatch.setattr(d, "_get", empty_repo)
    assert d._ci_of("effecet", "new-repo", "feat/x#1", "t") == "none"
    assert seen == ["/repos/effecet/new-repo/commits/feat%2Fx%231/check-runs?per_page=100"]


def test_ci_of_raises_on_other_api_errors(monkeypatch):
    def rate_limited(path, token):
        raise d.urllib.error.HTTPError(path, 403, "rate limit", {}, None)

    monkeypatch.setattr(d, "_get", rate_limited)
    with pytest.raises(d.urllib.error.HTTPError) as exc:
        d._ci_of("effecet", "repo", "main", "t")
    assert exc.value.code == 403


def test_ci_state_can_report_cancelled_separately():
    cancelled = {"status": "completed", "conclusion": "cancelled"}
    ok = {"status": "completed", "conclusion": "success"}
    failed = {"status": "completed", "conclusion": "failure"}
    assert d.ci_state([cancelled, ok], cancelled_is_failure=False) == "cancelled"
    assert d.ci_state([cancelled, ok]) == "fail"
    assert d.ci_state([cancelled, failed], cancelled_is_failure=False) == "fail"
    assert d.ci_state([cancelled, {"status": "queued"}], cancelled_is_failure=False) == "pending"


def test_render_health_shows_cancelled_as_a_warning_not_a_failure():
    out = d.render(digest(health={"ok": "pass", "starved": "cancelled"}), "effecet", NOW)
    assert "1/2 repos green" in out
    assert "⚠️ starved: CI cancelled" in out
    assert "CI failing" not in out


def test_collect_reports_cancelled_on_branches_but_fail_on_open_prs(monkeypatch):
    cancelled = {"check_runs": [{"status": "completed", "conclusion": "cancelled"}]}
    pr = {
        "number": 7,
        "title": "t",
        "html_url": "https://github.com/effecet/repo/pull/7",
        "user": {"login": "effecet"},
        "body": "",
        "head": {"sha": "abc"},
        "auto_merge": None,
    }

    def fake_get(path, token):
        if path.startswith("/users/"):
            return [{"name": "repo", "default_branch": "main"}]
        if "/pulls?" in path:
            return [pr]
        if "/check-runs" in path:
            return cancelled
        if path.startswith("/search/"):
            return {"total_count": 0, "items": []}
        raise AssertionError(path)

    monkeypatch.setattr(d, "_get", fake_get)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    got = d.collect("effecet", "t", NOW)
    assert got.health == {"repo": "cancelled"}
    assert [p.ci for p in got.open_prs] == ["fail"]


def test_render_health_lists_failures_before_softer_states():
    health = {f"a{i:02}": "cancelled" for i in range(d.HEALTH_SHOWN)}
    health["zz-broken"] = "fail"
    out = d.render(digest(health=health), "effecet", NOW)
    assert "❌ zz-broken: CI failing" in out
    assert out.index("zz-broken") < out.index("a00")
    assert "+1 more" in out


def test_lookup_tables_cover_every_ci_state():
    states = set(typing.get_args(d.CIState))
    assert set(d.SEVERITY) == set(d.ICON) == states
    assert set(d.HEALTH_NOTE) == states - {"pass"}


def test_render_health_lists_cancelled_before_running():
    out = d.render(digest(health={"a-busy": "pending", "b-starved": "cancelled"}), "effecet", NOW)
    assert out.index("b-starved: CI cancelled") < out.index("a-busy: CI running")
