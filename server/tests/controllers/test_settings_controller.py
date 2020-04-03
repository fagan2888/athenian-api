import json

import pytest
from sqlalchemy import delete, insert, select, update

from athenian.api.async_read_sql_query import read_sql_query
from athenian.api.models.state.models import Account, ReleaseSetting, RepositorySet, UserAccount
from athenian.api.models.web import ReleaseMatchSetting, ReleaseMatchStrategy


async def validate_settings(body, response, sdb, exhaustive: bool):
    assert response.status == 200
    repos = json.loads((await response.read()).decode("utf-8"))
    assert len(repos) > 0
    assert repos[0].startswith("github.com/")
    df = await read_sql_query(
        select([ReleaseSetting]), sdb, ReleaseSetting,
        index=[ReleaseSetting.repository.key, ReleaseSetting.account_id.key])
    if exhaustive:
        assert len(df) == len(repos)
    for r in repos:
        s = df.loc[r, body["account"]]
        assert s["branches"] == body["branches"]
        assert s["tags"] == body["tags"]
        assert s["match"] == (body["match"] == ReleaseMatchStrategy.TAG)
    return repos


async def test_set_release_match_overwrite(client, headers, sdb):
    body = {
        "repositories": ["{1}"],
        "account": 1,
        "branches": "master",
        "tags": ".*",
        "match": ReleaseMatchStrategy.TAG,
    }
    response = await client.request(
        method="PUT", path="/v1/settings/release_match", headers=headers, json=body)
    repos = await validate_settings(body, response, sdb, True)
    assert repos == ["github.com/src-d/gitbase", "github.com/src-d/go-git"]
    body.update({
        "branches": ".*",
        "tags": "v.*",
        "match": ReleaseMatchStrategy.BRANCH,
    })
    response = await client.request(
        method="PUT", path="/v1/settings/release_match", headers=headers, json=body)
    repos = await validate_settings(body, response, sdb, True)
    assert repos == ["github.com/src-d/gitbase", "github.com/src-d/go-git"]


async def test_set_release_match_different_accounts(client, headers, sdb):
    body1 = {
        "repositories": ["github.com/src-d/go-git"],
        "account": 1,
        "branches": "master",
        "tags": ".*",
        "match": ReleaseMatchStrategy.TAG,
    }
    response1 = await client.request(
        method="PUT", path="/v1/settings/release_match", headers=headers, json=body1)
    await sdb.execute(update(Account).where(Account.id == 1).values(
        {Account.installation_id.key: None}))
    await sdb.execute(update(Account).where(Account.id == 2).values(
        {Account.installation_id.key: 6366825}))
    await sdb.execute(update(UserAccount).where(UserAccount.account_id == 2).values(
        {UserAccount.is_admin.key: True}))
    body2 = {
        "repositories": ["github.com/src-d/go-git"],
        "account": 2,
        "branches": ".*",
        "tags": "v.*",
        "match": ReleaseMatchStrategy.BRANCH,
    }
    response2 = await client.request(
        method="PUT", path="/v1/settings/release_match", headers=headers, json=body2)
    await validate_settings(body1, response1, sdb, False)
    await validate_settings(body2, response2, sdb, False)


@pytest.mark.parametrize("code, account, repositories, branches, tags, match", [
    (400, 1, ["{1}"], None, ".*", ReleaseMatchStrategy.BRANCH),
    (400, 1, ["{1}"], "", ".*", ReleaseMatchStrategy.TAG_OR_BRANCH),
    (200, 1, ["{1}"], "", ".*", ReleaseMatchStrategy.TAG),
    (200, 1, [], "", ".*", ReleaseMatchStrategy.TAG),
    (400, 1, ["{1}"], None, ".*", ReleaseMatchStrategy.TAG),
    (400, 1, ["{1}"], "", ".*", ReleaseMatchStrategy.BRANCH),
    (400, 1, ["{1}"], "(f", ".*", ReleaseMatchStrategy.BRANCH),
    (400, 1, ["{1}"], ".*", None, ReleaseMatchStrategy.TAG),
    (400, 1, ["{1}"], ".*", None, ReleaseMatchStrategy.BRANCH),
    (200, 1, ["{1}"], ".*", "", ReleaseMatchStrategy.BRANCH),
    (400, 1, ["{1}"], ".*", "", ReleaseMatchStrategy.TAG),
    (400, 1, ["{1}"], ".*", "", ReleaseMatchStrategy.TAG_OR_BRANCH),
    (400, 1, ["{1}"], ".*", "(f", ReleaseMatchStrategy.TAG),
    (403, 2, ["{1}"], ".*", ".*", ReleaseMatchStrategy.BRANCH),
    (404, 3, ["{1}"], ".*", ".*", ReleaseMatchStrategy.BRANCH),
    (403, 1, ["{2}"], ".*", ".*", ReleaseMatchStrategy.BRANCH),
    (400, 1, ["{1}"], ".*", ".*", "whatever"),
    (400, 1, ["{1}"], ".*", ".*", None),
    (400, 1, ["{1}"], ".*", ".*", ""),
])
async def test_set_release_match_nasty_input(
        client, headers, code, account, repositories, branches, tags, match):
    body = {
        "repositories": repositories,
        "account": account,
        "branches": branches,
        "tags": tags,
        "match": match,
    }
    response = await client.request(
        method="PUT", path="/v1/settings/release_match", headers=headers, json=body)
    assert response.status == code


async def test_set_release_match_422(client, headers, sdb, gkwillie):
    await sdb.execute(delete(RepositorySet))
    await sdb.execute(update(Account)
                      .where(Account.id == 1)
                      .values({Account.installation_id.key: None}))
    await sdb.execute(insert(UserAccount).values(UserAccount(
        user_id="github|60340680", account_id=1, is_admin=True,
    ).create_defaults().explode(with_primary_keys=True)))
    body = {
        "repositories": [],
        "account": 1,
        "branches": ".*",
        "tags": ".*",
        "match": ReleaseMatchStrategy.TAG_OR_BRANCH,
    }
    response = await client.request(
        method="PUT", path="/v1/settings/release_match", headers=headers, json=body)
    assert response.status == 422


async def test_get_release_match_settings_defaults(client, headers):
    response = await client.request(
        method="GET", path="/v1/settings/release_match/1", headers=headers)
    assert response.status == 200
    settings = {k: ReleaseMatchSetting.from_dict(v)
                for k, v in json.loads((await response.read()).decode("utf-8")).items()}
    defset = ReleaseMatchSetting(
        branches="{{default}}",
        tags=".*",
        match=ReleaseMatchStrategy.TAG_OR_BRANCH,
    )
    assert settings["github.com/src-d/go-git"] == defset
    for k, v in settings.items():
        assert v == defset, k


async def test_get_release_match_settings_existing(client, headers, sdb):
    await sdb.execute(insert(ReleaseSetting).values(
        ReleaseSetting(repository="github.com/src-d/go-git",
                       account_id=1,
                       branches="master",
                       tags="v.*",
                       match=1).create_defaults().explode(with_primary_keys=True)))
    response = await client.request(
        method="GET", path="/v1/settings/release_match/1", headers=headers)
    assert response.status == 200
    settings = {k: ReleaseMatchSetting.from_dict(v)
                for k, v in json.loads((await response.read()).decode("utf-8")).items()}
    assert settings["github.com/src-d/go-git"] == ReleaseMatchSetting(
        branches="master",
        tags="v.*",
        match=ReleaseMatchStrategy.TAG,
    )
    defset = ReleaseMatchSetting(
        branches="{{default}}",
        tags=".*",
        match=ReleaseMatchStrategy.TAG_OR_BRANCH,
    )
    for k, v in settings.items():
        if k != "github.com/src-d/go-git":
            assert v == defset, k


@pytest.mark.parametrize("account, code", [(2, 422), (3, 404)])
async def test_get_release_match_settings_nasty_input(client, headers, account, code):
    response = await client.request(
        method="GET", path="/v1/settings/release_match/%d" % account, headers=headers)
    assert response.status == code