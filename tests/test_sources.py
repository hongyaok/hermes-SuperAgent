import pytest


def make_source(sa, allowed=("alice",)):
    return sa.sa_sources.GitHubSource(
        repo="acme/widgets",
        token="tok",
        agent_login="superagent-bot",
        allowed_users=list(allowed),
    )


def comment(*, id=1, login="alice", body="@superagent-bot fix the parser crash", issue=5, kind="User", created="2026-11-06T01:00:00Z"):
    return {
        "id": id,
        "user": {"login": login, "type": kind},
        "body": body,
        "issue_url": f"https://api.github.com/repos/acme/widgets/issues/{issue}",
        "created_at": created,
    }


def test_mention_becomes_an_inbound(sa):
    source = make_source(sa)
    comments = [comment()]
    source._request = lambda *args, **kwargs: comments

    inbound, cursor = source.poll()
    assert len(inbound) == 1
    assert inbound[0].request == "fix the parser crash"
    assert inbound[0].requester == "alice"
    assert inbound[0].issue_number == 5
    assert cursor == "2026-11-06T01:00:00Z"


def test_non_allowed_user_is_skipped(sa):
    source = make_source(sa)
    source._request = lambda *args, **kwargs: [comment(login="mallory")]
    inbound, _ = source.poll()
    assert inbound == []


def test_bot_and_self_are_skipped(sa):
    source = make_source(sa)
    source._request = lambda *args, **kwargs: [
        comment(id=1, login="ci-bot", kind="Bot"),
        comment(id=2, login="superagent-bot"),
    ]
    inbound, _ = source.poll()
    assert inbound == []


def test_comment_without_mention_is_skipped(sa):
    source = make_source(sa)
    source._request = lambda *args, **kwargs: [comment(body="fix the parser crash")]
    inbound, _ = source.poll()
    assert inbound == []


def test_missing_config_raises(sa):
    source = sa.sa_sources.GitHubSource(repo="", token="", agent_login="")
    with pytest.raises(sa.sa_sources.SourceError):
        source.poll()


def test_reply_posts_to_the_issue(sa):
    source = make_source(sa)
    calls = []

    def fake_request(method, path, payload=None):
        calls.append((method, path, payload))
        return {"id": 9}

    source._request = fake_request
    source.reply(5, "done")
    assert calls == [("POST", "/repos/acme/widgets/issues/5/comments", {"body": "done"})]
