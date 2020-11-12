from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from operator import itemgetter
from typing import Collection, Dict, Optional, Set

from aiohttp import ClientResponse
import dateutil
from prometheus_client import CollectorRegistry
import pytest
from sqlalchemy import delete, insert, select

from athenian.api import setup_cache_metrics
from athenian.api.controllers.features.entries import calc_pull_request_facts_github
from athenian.api.controllers.miners.filters import JIRAFilter, LabelFilter
from athenian.api.controllers.miners.types import Property
from athenian.api.defer import wait_deferred, with_defer
from athenian.api.models.metadata.github import Branch, Release
from athenian.api.models.precomputed.models import GitHubRelease
from athenian.api.models.state.models import AccountJiraInstallation, ReleaseSetting
from athenian.api.models.web import CommitsList, PullRequestSet
from athenian.api.models.web.filtered_label import FilteredLabel
from athenian.api.models.web.filtered_releases import FilteredReleases
from athenian.api.models.web.pull_request_participant import PullRequestParticipant
from athenian.api.models.web.pull_request_property import PullRequestProperty
from athenian.api.typing_utils import wraps
from tests.conftest import FakeCache, has_memcached


@pytest.mark.filter_repositories
async def test_filter_repositories_no_repos(client, headers):
    body = {
        "date_from": "2015-01-12",
        "date_to": "2015-01-12",
        "account": 1,
    }
    response = await client.request(
        method="POST", path="/v1/filter/repositories", headers=headers, json=body)
    assert response.status == 200
    repos = json.loads((await response.read()).decode("utf-8"))
    assert repos == []


@pytest.mark.filter_repositories
@with_defer
async def test_filter_repositories_smoke(client, headers, mdb, pdb, release_match_setting_tag):
    time_from = datetime(2017, 9, 15, tzinfo=timezone.utc)
    time_to = datetime(2017, 9, 18, tzinfo=timezone.utc)
    args = (time_from, time_to, {"src-d/go-git"}, {}, LabelFilter.empty(), JIRAFilter.empty(),
            False, release_match_setting_tag, False, mdb, pdb, None)
    await calc_pull_request_facts_github(*args)
    await wait_deferred()
    body = {
        "date_from": "2017-09-16",
        "date_to": "2017-09-17",
        "timezone": 60,
        "account": 1,
        "in": ["github.com/src-d/go-git"],
    }
    response = await client.request(
        method="POST", path="/v1/filter/repositories", headers=headers, json=body)
    repos = json.loads((await response.read()).decode("utf-8"))
    assert repos == ["github.com/src-d/go-git"]
    body["in"] = ["github.com/src-d/gitbase"]
    response = await client.request(
        method="POST", path="/v1/filter/repositories", headers=headers, json=body)
    repos = json.loads((await response.read()).decode("utf-8"))
    assert repos == []


@pytest.mark.filter_repositories
@with_defer
async def test_filter_repositories_exclude_inactive(
        client, headers, mdb, pdb, release_match_setting_tag):
    time_from = datetime(2017, 9, 15, tzinfo=timezone.utc)
    time_to = datetime(2017, 9, 18, tzinfo=timezone.utc)
    args = (time_from, time_to, {"src-d/go-git"}, {}, LabelFilter.empty(), JIRAFilter.empty(),
            False, release_match_setting_tag, False, mdb, pdb, None)
    await calc_pull_request_facts_github(*args)
    await wait_deferred()
    body = {
        "date_from": "2017-09-16",
        "date_to": "2017-09-17",
        "timezone": 60,
        "account": 1,
        "in": ["github.com/src-d/go-git"],
        "exclude_inactive": True,
    }
    response = await client.request(
        method="POST", path="/v1/filter/repositories", headers=headers, json=body)
    repos = json.loads((await response.read()).decode("utf-8"))
    assert repos == []


@pytest.mark.filter_repositories
async def test_filter_repositories_fuck_up(client, headers, sdb, pdb):
    await sdb.execute(insert(ReleaseSetting).values(
        ReleaseSetting(repository="github.com/src-d/go-git",
                       account_id=1,
                       branches="master",
                       tags=".*",
                       match=0).create_defaults().explode(with_primary_keys=True)))
    await pdb.execute(insert(GitHubRelease).values(
        GitHubRelease(id="1",
                      release_match="branch|whatever",
                      repository_full_name="src-d/go-git",
                      repository_node_id="repository_node_id",
                      name="release",
                      published_at=datetime(2017, 1, 1, hour=12, tzinfo=timezone.utc),
                      url="url",
                      sha="sha",
                      commit_id="commit_id").create_defaults().explode(with_primary_keys=True)))
    body = {
        "date_from": "2017-01-01",
        "date_to": "2017-01-01",
        "timezone": 60,
        "account": 1,
        "in": ["github.com/src-d/go-git"],
        "exclude_inactive": False,
    }
    response = await client.request(
        method="POST", path="/v1/filter/repositories", headers=headers, json=body)
    repos = json.loads((await response.read()).decode("utf-8"))
    assert repos == []


@pytest.mark.filter_repositories
@pytest.mark.parametrize("account, date_to, code",
                         [(3, "2020-01-23", 403), (10, "2020-01-23", 403), (1, "2015-10-13", 200),
                          (1, "2010-01-11", 400), (1, "2020-01-32", 400)])
async def test_filter_repositories_nasty_input(client, headers, account, date_to, code):
    body = {
        "date_from": "2015-10-13",
        "date_to": date_to,
        "account": account,
    }
    response = await client.request(
        method="POST", path="/v1/filter/repositories", headers=headers, json=body)
    assert response.status == code


@pytest.mark.filter_contributors
@pytest.mark.parametrize("in_", [{}, {"in": []}])
async def test_filter_contributors_no_repos(client, headers, in_):
    body = {
        "date_from": "2015-01-12",
        "date_to": "2020-01-23",
        "account": 1,
        **in_,
    }
    response = await client.request(
        method="POST", path="/v1/filter/contributors", headers=headers, json=body)
    contribs = json.loads((await response.read()).decode("utf-8"))
    assert len(contribs) == 202
    assert len(set(c["login"] for c in contribs)) == len(contribs)
    assert all(c["login"].startswith("github.com/") for c in contribs)
    contribs = {c["login"]: c for c in contribs}
    assert "github.com/mcuadros" in contribs
    body["date_to"] = body["date_from"]
    response = await client.request(
        method="POST", path="/v1/filter/contributors", headers=headers, json=body)
    assert response.status == 200
    contribs = json.loads((await response.read()).decode("utf-8"))
    assert contribs == []


@pytest.mark.filter_contributors
async def test_filter_contributors(client, headers):
    body = {
        "date_from": "2015-10-13",
        "date_to": "2020-01-23",
        "timezone": 60,
        "account": 1,
        "in": ["github.com/src-d/go-git"],
    }
    response = await client.request(
        method="POST", path="/v1/filter/contributors", headers=headers, json=body)
    contribs = json.loads((await response.read()).decode("utf-8"))
    assert len(contribs) == 199
    assert len(set(c["login"] for c in contribs)) == len(contribs)
    assert all(c["login"].startswith("github.com/") for c in contribs)
    contribs = {c["login"]: c for c in contribs}
    assert "github.com/mcuadros" in contribs
    assert "github.com/author_login" not in contribs
    assert "github.com/committer_login" not in contribs
    assert contribs["github.com/mcuadros"]["avatar"]
    assert contribs["github.com/mcuadros"]["name"] == "Máximo Cuadros"
    topics = set()
    for c in contribs.values():
        for v in c["updates"]:
            topics.add(v)
    assert topics == {"prs", "commenter", "commit_author", "commit_committer", "reviewer",
                      "releaser"}
    body["in"] = ["github.com/src-d/gitbase"]
    response = await client.request(
        method="POST", path="/v1/filter/contributors", headers=headers, json=body)
    contribs = json.loads((await response.read()).decode("utf-8"))
    assert contribs == []


@pytest.mark.filter_contributors
async def test_filter_contributors_merger_only(client, headers):
    body = {
        "date_from": "2015-10-13",
        "date_to": "2020-01-23",
        "timezone": 60,
        "account": 1,
        "in": ["github.com/src-d/go-git"],
        "as": ["merger"],
    }
    response = await client.request(
        method="POST", path="/v1/filter/contributors", headers=headers, json=body)
    mergers = json.loads((await response.read()).decode("utf-8"))
    mergers_logins = {c["login"] for c in mergers}

    assert len(mergers) == 8
    assert len(mergers_logins) == len(mergers)
    assert all(x.startswith("github.com/") for x in mergers_logins)

    expected_mergers = {"github.com/ajnavarro",
                        "github.com/alcortesm",
                        "github.com/erizocosmico",
                        "github.com/jfontan",
                        "github.com/mcuadros",
                        "github.com/orirawlings",
                        "github.com/smola",
                        "github.com/strib"}
    assert mergers_logins == expected_mergers


@pytest.mark.filter_contributors
async def test_filter_contributors_with_empty_and_full_roles(client, headers):
    all_roles = ["author", "reviewer", "commit_author", "commit_committer",
                 "commenter", "merger", "releaser"]

    base_body = {
        "date_from": "2015-10-13",
        "date_to": "2020-01-23",
        "timezone": 60,
        "account": 1,
        "in": ["github.com/src-d/go-git"],
    }

    body_empty_roles = {**base_body, "as": []}
    body_all_roles = {**base_body, "as": all_roles}

    response_empty_roles = await client.request(
        method="POST", path="/v1/filter/contributors", headers=headers, json=body_empty_roles)
    response_all_roles = await client.request(
        method="POST", path="/v1/filter/contributors", headers=headers, json=body_all_roles)

    parsed_empty_roles = json.loads((await response_empty_roles.read()).decode("utf-8"))
    parsed_all_roles = json.loads((await response_all_roles.read()).decode("utf-8"))

    assert (sorted(parsed_empty_roles, key=itemgetter("login")) ==
            sorted(parsed_all_roles, key=itemgetter("login")))


@pytest.mark.filter_contributors
@pytest.mark.parametrize("account, date_to, code",
                         [(3, "2020-01-23", 403), (10, "2020-01-23", 403), (1, "2015-10-13", 200),
                          (1, "2010-01-11", 400), (1, "2020-01-32", 400)])
async def test_filter_contributors_nasty_input(client, headers, account, date_to, code):
    body = {
        "date_from": "2015-10-13",
        "date_to": date_to,
        "account": account,
    }
    response = await client.request(
        method="POST", path="/v1/filter/contributors", headers=headers, json=body)
    assert response.status == code


@pytest.fixture(scope="module")
def filter_prs_single_prop_cache():
    fc = FakeCache()
    setup_cache_metrics(fc, {}, CollectorRegistry(auto_describe=True))
    for v in fc.metrics["context"].values():
        v.set(defaultdict(int))
    return fc


def with_only_master_branch(func):
    async def wrapped_with_only_master_branch(**kwargs):
        mdb = kwargs["mdb"]
        branches = await mdb.fetch_all(select([Branch]).where(Branch.branch_name != "master"))
        await mdb.execute(delete(Branch).where(Branch.branch_name != "master"))
        try:
            await func(**kwargs)
        finally:
            for branch in branches:
                await mdb.execute(insert(Branch).values(branch))

    return wraps(wrapped_with_only_master_branch, func)


@pytest.mark.filter_pull_requests
@pytest.mark.parametrize("prop", [k.name.lower() for k in Property])
@with_only_master_branch
async def test_filter_prs_single_prop(
        # do not remove "mdb", it is required by the decorators
        client, headers, mdb, prop, app, filter_prs_single_prop_cache):
    app._cache = filter_prs_single_prop_cache
    body = {
        "date_from": "2015-10-13",
        "date_to": "2020-04-23",
        "account": 1,
        "in": [],
        "properties": [prop],
        "exclude_inactive": False,
    }
    response = await client.request(
        method="POST", path="/v1/filter/pull_requests", headers=headers, json=body)
    await validate_prs_response(response, {prop}, {},
                                datetime(year=2020, month=4, day=23, tzinfo=timezone.utc))


@pytest.mark.filter_pull_requests
@with_only_master_branch
async def test_filter_prs_all_properties(client, headers, mdb):
    body = {
        "date_from": "2015-10-13",
        "date_to": "2020-04-23",
        "updated_from": "2015-10-13",
        "updated_to": "2020-05-01",
        "timezone": 60,
        "account": 1,
        "in": [],
        "properties": [],
        "exclude_inactive": False,
    }
    time_to = datetime(year=2020, month=4, day=23, tzinfo=timezone.utc)
    response = await client.request(
        method="POST", path="/v1/filter/pull_requests", headers=headers, json=body)
    await validate_prs_response(response, set(PullRequestProperty), {}, time_to, 682)
    del body["properties"]
    response = await client.request(
        method="POST", path="/v1/filter/pull_requests", headers=headers, json=body)
    assert response.status == 200


@pytest.mark.filter_pull_requests
@with_only_master_branch
async def test_filter_prs_shot_updated(client, headers, mdb):
    body = {
        "date_from": "2016-10-13",
        "date_to": "2018-01-23",
        "timezone": 60,
        "account": 1,
        "in": [],
        "properties": [PullRequestProperty.MERGE_HAPPENED],
        "with": {
            "author": ["github.com/mcuadros"],
        },
        "updated_from": "2017-01-01",
        "updated_to": "2018-01-24",
        "exclude_inactive": False,
    }
    time_to = datetime(year=2018, month=1, day=24, tzinfo=timezone.utc)
    response = await client.request(
        method="POST", path="/v1/filter/pull_requests", headers=headers, json=body)
    n = await validate_prs_response(response, {PullRequestProperty.MERGE_HAPPENED},
                                    {"author": ["github.com/mcuadros"]}, time_to)
    assert n == 52  # it is 75 without the constraints


@pytest.mark.filter_pull_requests
async def test_filter_prs_labels_include(client, headers):
    body = {
        "date_from": "2018-09-01",
        "date_to": "2018-11-30",
        "timezone": 0,
        "account": 1,
        "in": [],
        "properties": [PullRequestProperty.MERGE_HAPPENED],
        "labels_include": ["bug"],
        "exclude_inactive": False,
    }
    response = await client.request(
        method="POST", path="/v1/filter/pull_requests", headers=headers, json=body)
    assert response.status == 200
    prs = PullRequestSet.from_dict(json.loads((await response.read()).decode("utf-8")))
    assert len(prs.data) == 2
    for pr in prs.data:
        assert "bug" in {label.name for label in pr.labels}


@pytest.mark.filter_pull_requests
@pytest.mark.parametrize("timezone, must_match", [(120, True), (60, True), (0, False)])
async def test_filter_prs_merged_timezone(client, headers, timezone, must_match):
    body = {
        "date_from": "2017-07-08",
        "date_to": "2017-07-10",
        "timezone": timezone,
        "account": 1,
        "in": [],
        "properties": [PullRequestProperty.MERGE_HAPPENED],
        "exclude_inactive": False,
    }
    response = await client.request(
        method="POST", path="/v1/filter/pull_requests", headers=headers, json=body)
    assert response.status == 200
    obj = json.loads((await response.read()).decode("utf-8"))
    prs = PullRequestSet.from_dict(obj)  # type: PullRequestSet
    matched = False
    for pr in prs.data:
        if pr.number == 467:  # merged 2017-07-08 01:37 GMT+2 = 2017-07-07 23:37 UTC
            matched = True
    assert matched == must_match


@pytest.mark.filter_pull_requests
@pytest.mark.parametrize("timezone, must_match", [(-7 * 60, False), (-8 * 60, True)])
async def test_filter_prs_created_timezone(client, headers, timezone, must_match):
    body = {
        "date_from": "2017-07-15",
        "date_to": "2017-07-16",
        "timezone": timezone,
        "account": 1,
        "in": [],
        "properties": [],
        "exclude_inactive": False,
    }
    response = await client.request(
        method="POST", path="/v1/filter/pull_requests", headers=headers, json=body)
    assert response.status == 200
    obj = json.loads((await response.read()).decode("utf-8"))
    prs = PullRequestSet.from_dict(obj)  # type: PullRequestSet
    matched = False
    for pr in prs.data:
        if pr.number == 485:  # created 2017-07-17 09:02 GMT+2 = 2017-07-17 07:02 UTC
            matched = True
    assert matched == must_match


async def test_filter_prs_jira(client, headers, app, filter_prs_single_prop_cache):
    app._cache = filter_prs_single_prop_cache
    body = {
        "date_from": "2015-10-13",
        "date_to": "2020-04-23",
        "account": 1,
        "in": [],
        "properties": [PullRequestProperty.MERGE_HAPPENED],
        "exclude_inactive": False,
    }
    if len(filter_prs_single_prop_cache.mem) == 0:
        response = await client.request(
            method="POST", path="/v1/filter/pull_requests", headers=headers, json=body)
        text = (await response.read()).decode("utf-8")
        assert response.status == 200, text
    body["jira"] = {
        "epics": ["DEV-149", "DEV-776", "DEV-737", "DEV-667", "DEV-140"],
        "labels_include": ["performance", "enhancement"],
        "labels_exclude": ["security"],
        "issue_types": ["Task"],
    }
    response = await client.request(
        method="POST", path="/v1/filter/pull_requests", headers=headers, json=body)
    text = (await response.read()).decode("utf-8")
    assert response.status == 200, text
    prs = PullRequestSet.from_dict(json.loads(text))
    data1 = prs.data
    assert len(prs.data) == 2
    filter_prs_single_prop_cache.mem.clear()
    response = await client.request(
        method="POST", path="/v1/filter/pull_requests", headers=headers, json=body)
    text = (await response.read()).decode("utf-8")
    assert response.status == 200, text
    prs = PullRequestSet.from_dict(json.loads(text))
    data2 = prs.data
    assert data1 == data2


open_go_git_pr_numbers = {
    570, 816, 970, 1273, 1069, 1086, 1098, 1139, 1152, 1153, 1173, 1238, 1243, 1246, 1254, 1270,
    1269, 1272, 1286, 1291, 1285,
}

rejected_go_git_pr_numbers = {
    3, 8, 75, 13, 46, 52, 53, 86, 103, 85, 101, 119, 127, 129, 156, 154, 257, 291, 272, 280, 281,
    353, 329, 330, 382, 383, 474, 392, 399, 407, 494, 419, 420, 503, 437, 446, 1186, 497, 486,
    506, 560, 548, 575, 591, 619, 639, 670, 671, 689, 743, 699, 715, 768, 776, 782, 790, 789, 824,
    800, 805, 819, 821, 849, 861, 863, 926, 1185, 867, 872, 880, 878, 1188, 908, 946, 940, 947,
    952, 951, 975, 1007, 1010, 976, 988, 997, 1003, 1002, 1016, 1104, 1120, 1044, 1062, 1075, 1078,
    1109, 1103, 1122, 1187, 1182, 1168, 1170, 1183, 1184, 1213, 1248, 1247, 1265, 1276,
}

force_push_dropped_go_git_pr_numbers = {
    504, 561, 907,
    1, 2, 5, 6, 7, 9, 10, 11, 12, 14, 15, 20, 16, 17, 18, 21, 22, 23, 25, 26, 24, 27, 28, 30, 32,
    34, 35, 37, 39, 47, 54, 56, 55, 58, 61, 64, 66, 63, 68, 69, 70, 74, 78, 79, 83, 84, 87, 88, 89,
    92, 93, 94, 95, 96, 97, 90, 91, 104, 105, 106, 108, 99, 100, 102, 116, 117, 118, 109, 110, 111,
    112, 113, 114, 115, 124, 121, 122, 130, 131, 132, 133, 135, 145, 146, 147, 148, 149, 150, 151,
    153, 136, 138, 140, 141, 142, 143, 144, 157, 158, 159, 160, 161, 162, 163, 164, 165, 176, 177,
    178, 179, 180, 181, 182, 183, 185, 186, 187, 188, 166, 167, 168, 169, 170, 171, 172, 173, 174,
    175, 190, 191, 192, 189, 200, 201, 204, 205, 207, 209, 210, 212, 213, 214, 215, 218, 219, 221,
    224, 227, 229, 230, 237, 240, 241, 233, 235, 244,
}

will_never_be_released_go_git_pr_numbers = {
    1180, 1195, 1204, 1205, 1206, 1208, 1214, 1225, 1226, 1235, 1231,
}


async def validate_prs_response(response: ClientResponse,
                                props: Set[str],
                                parts: Dict[str, Collection[str]],
                                time_to: datetime,
                                count: Optional[int] = None) -> int:
    text = (await response.read()).decode("utf-8")
    assert response.status == 200, text
    obj = json.loads(text)
    prs = PullRequestSet.from_dict(obj)
    users = prs.include.users
    assert len(users) > 0, text
    for user in users:
        assert user.startswith("github.com/")
        assert len(user.split("/")) == 2
    assert len(prs.data) > 0, text
    numbers = set()
    total_comments = total_commits = total_review_comments = total_released = total_rejected = \
        total_review_requests = total_reviews = total_force_push_dropped = 0
    tdz = timedelta(0)
    timings = defaultdict(lambda: tdz)
    if count is not None:
        assert len(prs.data) == count
    for pr in prs.data:
        assert pr.title
        assert pr.repository == "github.com/src-d/go-git", str(pr)

        assert pr.number > 0, str(pr)
        assert pr.number not in numbers, str(pr)
        numbers.add(pr.number)

        # >= because there are closed PRs with 0 commits
        assert pr.size_added >= 0, str(pr)
        assert pr.size_removed >= 0, str(pr)
        assert pr.files_changed >= 0, str(pr)
        total_comments += pr.comments
        total_commits += pr.commits
        total_review_comments += pr.review_comments
        total_reviews += pr.reviews
        if pr.files_changed > 0:
            assert pr.commits > 0, str(pr)
        if pr.size_added > 0 or pr.size_removed > 0:
            assert pr.files_changed > 0, str(pr)
        if pr.review_comments > 0:
            assert pr.reviews > 0, str(pr)

        assert pr.created, str(pr)
        assert pr.created < time_to
        if pr.closed is None:
            assert pr.merged is None
        else:
            assert pr.closed > pr.created
        if pr.merged:
            assert pr.closed is not None
            assert abs(pr.merged - pr.closed) < timedelta(seconds=60)
        if pr.review_requested is not None:
            assert PullRequestProperty.REVIEW_REQUEST_HAPPENED in pr.properties, str(pr)
        if PullRequestProperty.REVIEW_REQUEST_HAPPENED in pr.properties:
            assert pr.review_requested is not None
            assert pr.review_requested >= pr.created, str(pr)
        if pr.first_review is not None:
            assert PullRequestProperty.REVIEW_HAPPENED in pr.properties, str(pr)
            assert pr.reviews > 0, str(pr)
            assert pr.first_review > pr.created, str(pr)
        if pr.reviews > 0:
            assert pr.first_review is not None, str(pr)
        if pr.approved is not None:
            assert pr.first_review <= pr.approved, str(pr)

        assert props.intersection(set(pr.properties)), str(pr)
        assert PullRequestProperty.CREATED in pr.properties, str(pr)
        if pr.number not in open_go_git_pr_numbers:
            assert pr.closed is not None
            if pr.number not in will_never_be_released_go_git_pr_numbers:
                assert PullRequestProperty.DONE in pr.properties, str(pr)
            else:
                assert PullRequestProperty.MERGE_HAPPENED in pr.properties
            if pr.number not in rejected_go_git_pr_numbers and \
                    pr.number not in will_never_be_released_go_git_pr_numbers and \
                    pr.number not in force_push_dropped_go_git_pr_numbers:
                assert PullRequestProperty.RELEASE_HAPPENED in pr.properties, str(pr)
            else:
                assert PullRequestProperty.RELEASE_HAPPENED not in pr.properties
                if pr.number in rejected_go_git_pr_numbers:
                    assert PullRequestProperty.MERGE_HAPPENED not in pr.properties, str(pr)
                if pr.number in force_push_dropped_go_git_pr_numbers:
                    assert PullRequestProperty.FORCE_PUSH_DROPPED in pr.properties, str(pr)
                    assert PullRequestProperty.MERGE_HAPPENED in pr.properties, str(pr)
        else:
            assert pr.closed is None

        if PullRequestProperty.WIP in pr.properties:
            assert PullRequestProperty.COMMIT_HAPPENED in pr.properties, str(pr)
        if PullRequestProperty.REVIEWING in pr.properties:
            assert PullRequestProperty.COMMIT_HAPPENED in pr.properties, str(pr)
            assert pr.stage_timings.review is not None
        total_review_requests += PullRequestProperty.REVIEW_REQUEST_HAPPENED in pr.properties
        if PullRequestProperty.MERGING in pr.properties:
            assert PullRequestProperty.APPROVE_HAPPENED in pr.properties, str(pr)
            assert pr.stage_timings.merge is not None
        if PullRequestProperty.RELEASING in pr.properties:
            assert PullRequestProperty.MERGE_HAPPENED in pr.properties, str(pr)
            assert PullRequestProperty.COMMIT_HAPPENED in pr.properties, str(pr)
            assert pr.stage_timings.release is not None, str(pr)
        if PullRequestProperty.DONE in pr.properties:
            assert pr.closed is not None, str(pr)
            if PullRequestProperty.MERGE_HAPPENED in pr.properties:
                if pr.number not in force_push_dropped_go_git_pr_numbers:
                    assert PullRequestProperty.RELEASE_HAPPENED in pr.properties, str(pr)
                else:
                    assert PullRequestProperty.FORCE_PUSH_DROPPED in pr.properties, str(pr)
                    total_force_push_dropped += 1
            else:
                assert PullRequestProperty.REJECTION_HAPPENED in pr.properties, str(pr)
                total_rejected += 1

        assert pr.stage_timings.wip is not None, str(pr)
        if pr.stage_timings.wip == tdz:
            if pr.stage_timings.merge is None:
                # review requested at once, no new commits, not merged
                assert pr.stage_timings.review > tdz, str(pr)
            else:
                # no new commits after opening the PR
                assert pr.stage_timings.merge > tdz, str(pr)
        else:
            assert pr.stage_timings.wip > tdz, str(pr)
        assert pr.stage_timings.review is None or pr.stage_timings.review >= tdz
        assert pr.stage_timings.merge is None or pr.stage_timings.merge >= tdz
        assert pr.stage_timings.release is None or pr.stage_timings.release >= tdz
        timings["wip"] += pr.stage_timings.wip
        if pr.stage_timings.review is not None:
            timings["review"] += pr.stage_timings.review
        if pr.stage_timings.merge is not None:
            timings["merge"] += pr.stage_timings.merge
        if pr.stage_timings.release is not None:
            timings["release"] += pr.stage_timings.release

        if PullRequestProperty.REVIEW_HAPPENED in pr.properties:
            # pr.review_comments can be 0
            assert pr.stage_timings.review is not None
        if pr.review_comments > 0:
            assert PullRequestProperty.REVIEW_HAPPENED in pr.properties, str(pr)
        if PullRequestProperty.APPROVE_HAPPENED in pr.properties:
            assert PullRequestProperty.REVIEW_HAPPENED in pr.properties, str(pr)
        if PullRequestProperty.CHANGES_REQUEST_HAPPENED in pr.properties:
            assert PullRequestProperty.REVIEW_HAPPENED in pr.properties, str(pr)
        if PullRequestProperty.MERGE_HAPPENED not in pr.properties and pr.closed is not None:
            assert PullRequestProperty.DONE in pr.properties
            if pr.stage_timings.merge is None:
                # https://github.com/src-d/go-git/pull/878
                assert pr.commits == 0, str(pr)
            else:
                assert pr.stage_timings.merge > tdz or pr.stage_timings.review > tdz, str(pr)
        if PullRequestProperty.RELEASE_HAPPENED in pr.properties:
            assert PullRequestProperty.DONE in pr.properties, str(pr)
            assert pr.released is not None, str(pr)
            assert pr.stage_timings.merge is not None, str(pr)
            assert pr.stage_timings.release is not None
        if pr.released is not None:
            if pr.number not in force_push_dropped_go_git_pr_numbers:
                assert PullRequestProperty.RELEASE_HAPPENED in pr.properties, str(pr)
                assert pr.release_url, str(pr)
            else:
                assert PullRequestProperty.FORCE_PUSH_DROPPED in pr.properties, str(pr)
                assert pr.release_url is None, str(pr)
            assert PullRequestProperty.DONE in pr.properties, str(pr)
            total_released += 1

        assert len(pr.participants) > 0
        authors = 0
        reviewers = 0
        mergers = 0
        releasers = 0
        inverse_participants = defaultdict(set)
        for p in pr.participants:
            assert p.id.startswith("github.com/")
            is_author = PullRequestParticipant.STATUS_AUTHOR in p.status
            authors += is_author
            if is_author:
                assert PullRequestParticipant.STATUS_REVIEWER not in p.status, pr.number
            reviewers += PullRequestParticipant.STATUS_REVIEWER in p.status
            mergers += PullRequestParticipant.STATUS_MERGER in p.status
            releasers += PullRequestParticipant.STATUS_RELEASER in p.status
            for s in p.status:
                inverse_participants[s].add(p.id)
        if pr.number != 749:
            # the author of 749 is deleted on GitHub
            assert authors == 1
        if reviewers == 0:
            assert PullRequestProperty.REVIEW_HAPPENED not in pr.properties
            assert PullRequestProperty.APPROVE_HAPPENED not in pr.properties
            assert PullRequestProperty.CHANGES_REQUEST_HAPPENED not in pr.properties
        else:
            assert PullRequestProperty.REVIEW_HAPPENED in pr.properties
        assert mergers <= 1
        if mergers == 1:
            assert PullRequestProperty.MERGE_HAPPENED in pr.properties
        assert releasers <= 1
        if releasers == 1:
            assert PullRequestProperty.RELEASE_HAPPENED in pr.properties
        if parts:
            passed = False
            for role, p in parts.items():
                passed |= bool(inverse_participants[role].intersection(set(p)))
            assert passed
        # we cannot cover all possible cases while keeping the test run time reasonable :(

    assert total_comments > 0
    assert total_commits > 0
    if props not in ({PullRequestProperty.WIP}, {PullRequestProperty.MERGING}):
        assert total_review_comments > 0
    else:
        assert total_review_comments == 0
    if props != {PullRequestProperty.WIP}:
        assert total_reviews > 0
    if props not in ({PullRequestProperty.RELEASE_HAPPENED}, {PullRequestProperty.MERGE_HAPPENED},
                     {PullRequestProperty.RELEASING}, {PullRequestProperty.MERGING},
                     {PullRequestProperty.REVIEWING}, {PullRequestProperty.WIP},
                     {PullRequestProperty.FORCE_PUSH_DROPPED}):
        assert total_rejected > 0
    else:
        assert total_rejected == 0
    if props not in ({PullRequestProperty.RELEASING}, {PullRequestProperty.MERGING},
                     {PullRequestProperty.REJECTION_HAPPENED}, {PullRequestProperty.REVIEWING},
                     {PullRequestProperty.WIP}, {PullRequestProperty.FORCE_PUSH_DROPPED}):
        assert total_released > 0
    else:
        assert total_released == 0
    if {PullRequestProperty.REVIEWING, PullRequestProperty.CHANGES_REQUEST_HAPPENED,
            PullRequestProperty.REVIEW_HAPPENED, PullRequestProperty.APPROVE_HAPPENED,
            PullRequestProperty.CHANGES_REQUEST_HAPPENED}.intersection(props):
        assert total_review_requests > 0
    if PullRequestProperty.FORCE_PUSH_DROPPED in props:
        assert total_force_push_dropped > 0
    for k, v in timings.items():
        assert v > tdz, k
    return len(prs.data)


@pytest.mark.filter_pull_requests
@pytest.mark.parametrize("account, date_to, updated_from, code",
                         [(3, "2020-01-23", None, 403),
                          (10, "2020-01-23", None, 403),
                          (1, "2015-10-13", None, 200),
                          (1, "2010-01-11", None, 400),
                          (1, "2020-01-32", None, 400),
                          (1, "2015-10-13", "2015-10-15", 400)])
async def test_filter_prs_nasty_input(client, headers, account, date_to, updated_from, code):
    body = {
        "date_from": "2015-10-13",
        "date_to": date_to,
        "account": account,
        "in": [],
        "properties": [],
        "exclude_inactive": False,
    }
    if updated_from is not None:
        body["updated_from"] = updated_from
    response = await client.request(
        method="POST", path="/v1/filter/pull_requests", headers=headers, json=body)
    assert response.status == code


@pytest.mark.filter_pull_requests
async def test_filter_prs_david_bug(client, headers):
    body = {
        "account": 1,
        "date_from": "2019-02-22",
        "date_to": "2020-02-22",
        "in": ["github.com/src-d/go-git"],
        "properties": ["wip", "reviewing", "merging", "releasing"],
        "with": {
            "author": ["github.com/Junnplus"],
            "reviewer": ["github.com/Junnplus"],
            "commit_author": ["github.com/Junnplus"],
            "commit_committer": ["github.com/Junnplus"],
            "commenter": ["github.com/Junnplus"],
            "merger": ["github.com/Junnplus"],
        },
        "exclude_inactive": False,
    }
    response = await client.request(
        method="POST", path="/v1/filter/pull_requests", headers=headers, json=body)
    assert response.status == 200


@pytest.mark.filter_pull_requests
async def test_filter_prs_developer_filter(client, headers):
    body = {
        "date_from": "2017-07-15",
        "date_to": "2017-12-16",
        "account": 1,
        "in": [],
        "properties": [],
        "with": {
            "author": ["github.com/mcuadros"],
        },
        "exclude_inactive": False,
    }
    response = await client.request(
        method="POST", path="/v1/filter/pull_requests", headers=headers, json=body)
    assert response.status == 200
    obj = json.loads((await response.read()).decode("utf-8"))
    prs = PullRequestSet.from_dict(obj)
    assert len(prs.data) == 27
    for pr in prs.data:
        passed = False
        for part in pr.participants:
            if part.id == "github.com/mcuadros":
                assert PullRequestParticipant.STATUS_AUTHOR in part.status
                passed = True
        assert passed


@pytest.mark.filter_pull_requests
async def test_filter_prs_exclude_inactive(client, headers):
    body = {
        "date_from": "2017-01-01",
        "date_to": "2017-01-11",
        "account": 1,
        "in": [],
        "properties": [],
        "exclude_inactive": True,
    }
    response = await client.request(
        method="POST", path="/v1/filter/pull_requests", headers=headers, json=body)
    assert response.status == 200
    obj = json.loads((await response.read()).decode("utf-8"))
    prs = PullRequestSet.from_dict(obj)
    assert len(prs.data) == 6


def skip_if_no_memcached(func):
    async def wrapped_skip_if_no_memcached(**kwargs):
        if kwargs["cached"]:
            if not has_memcached:
                raise pytest.skip("no memcached")
            kwargs["app"]._cache = kwargs["client_cache"]
        return await func(**kwargs)

    wraps(wrapped_skip_if_no_memcached, func)
    return wrapped_skip_if_no_memcached


@pytest.mark.filter_commits
@pytest.mark.parametrize("cached", [False, True], ids=["no cache", "with cache"])
@skip_if_no_memcached
async def test_filter_commits_bypassing_prs_mcuadros(client, cached, headers, app, client_cache):
    body = {
        "account": 1,
        "date_from": "2019-01-12",
        "date_to": "2020-02-22",
        "in": ["{1}"],
        "property": "bypassing_prs",
        "with_author": ["github.com/mcuadros"],
        "with_committer": ["github.com/mcuadros"],
    }
    response = await client.request(
        method="POST", path="/v1/filter/commits", headers=headers, json=body)
    assert response.status == 200
    commits = CommitsList.from_dict(json.loads((await response.read()).decode("utf-8")))
    assert commits.to_dict() == {
        "data": [{"author": {"email": "mcuadros@gmail.com",
                             "login": "github.com/mcuadros",
                             "name": "Máximo Cuadros",
                             "timestamp": datetime(2019, 4, 24, 13, 20, 51, tzinfo=timezone.utc),
                             "timezone": 2.0},
                  "committer": {"email": "mcuadros@gmail.com",
                                "login": "github.com/mcuadros",
                                "name": "Máximo Cuadros",
                                "timestamp": datetime(2019, 4, 24, 13, 20, 51,
                                                      tzinfo=timezone.utc),
                                "timezone": 2.0},
                  "files_changed": 1,
                  "hash": "5c6d199dc675465f5e103ea36c0bfcb9d3ebc565",
                  "message": "plumbing: commit.Stats, fix panic on empty chucks\n\n"
                             "Signed-off-by: Máximo Cuadros <mcuadros@gmail.com>",
                  "repository": "src-d/go-git",
                  "size_added": 4,
                  "size_removed": 0}],
        "include": {"users": {
            "github.com/mcuadros": {
                "avatar": "https://avatars0.githubusercontent.com/u/1573114?s=600&v=4"}}}}


@pytest.mark.filter_commits
@pytest.mark.parametrize("cached", [False, True], ids=["no cache", "with cache"])
@skip_if_no_memcached
async def test_filter_commits_no_pr_merges_mcuadros(client, cached, headers, app, client_cache):
    body = {
        "account": 1,
        "date_from": "2019-01-12",
        "date_to": "2020-02-22",
        "timezone": 60,
        "in": ["{1}"],
        "property": "no_pr_merges",
        "with_author": ["github.com/mcuadros"],
        "with_committer": ["github.com/mcuadros"],
    }
    response = await client.request(
        method="POST", path="/v1/filter/commits", headers=headers, json=body)
    assert response.status == 200
    commits = CommitsList.from_dict(json.loads((await response.read()).decode("utf-8")))
    assert len(commits.data) == 6
    assert len(commits.include.users) == 1
    for c in commits.data:
        assert c.author.login == "github.com/mcuadros"
        assert c.committer.login == "github.com/mcuadros"


@pytest.mark.filter_commits
@pytest.mark.parametrize("cached", [False, True], ids=["no cache", "with cache"])
@skip_if_no_memcached
async def test_filter_commits_bypassing_prs_merges(client, cached, headers, app, client_cache):
    body = {
        "account": 1,
        "date_from": "2019-01-12",
        "date_to": "2020-02-22",
        "in": ["{1}"],
        "property": "bypassing_prs",
        "with_author": [],
        "with_committer": [],
    }
    response = await client.request(
        method="POST", path="/v1/filter/commits", headers=headers, json=body)
    assert response.status == 200
    commits = CommitsList.from_dict(json.loads((await response.read()).decode("utf-8")))
    assert len(commits.data) == 25
    for c in commits.data:
        assert c.committer.email != "noreply@github.com"


@pytest.mark.filter_commits
@pytest.mark.parametrize("cached", [False, True], ids=["no cache", "with cache"])
@skip_if_no_memcached
async def test_filter_commits_bypassing_prs_empty(client, cached, headers, app, client_cache):
    body = {
        "account": 1,
        "date_from": "2020-01-12",
        "date_to": "2020-02-22",
        "in": ["{1}"],
        "property": "bypassing_prs",
        "with_author": ["github.com/mcuadros"],
        "with_committer": ["github.com/mcuadros"],
    }
    response = await client.request(
        method="POST", path="/v1/filter/commits", headers=headers, json=body)
    assert response.status == 200
    commits = CommitsList.from_dict(json.loads((await response.read()).decode("utf-8")))
    assert len(commits.data) == 0
    assert len(commits.include.users) == 0


@pytest.mark.filter_commits
@pytest.mark.parametrize("cached", [False, True], ids=["no cache", "with cache"])
@skip_if_no_memcached
async def test_filter_commits_bypassing_prs_no_with(client, cached, headers, app, client_cache):
    body = {
        "account": 1,
        "date_from": "2020-01-12",
        "date_to": "2020-02-21",
        "in": ["{1}"],
        "property": "bypassing_prs",
    }
    response = await client.request(
        method="POST", path="/v1/filter/commits", headers=headers, json=body)
    assert response.status == 200
    commits = CommitsList.from_dict(
        json.loads((await response.read()).decode("utf-8")))
    assert len(commits.data) == 0
    assert len(commits.include.users) == 0
    body["date_to"] = "2020-02-22"
    response = await client.request(
        method="POST", path="/v1/filter/commits", headers=headers, json=body)
    assert response.status == 200
    commits = CommitsList.from_dict(json.loads((await response.read()).decode("utf-8")))
    assert len(commits.data) == 1
    assert commits.data[0].committer.timestamp == datetime(2020, 2, 22, 18, 58, 50,
                                                           tzinfo=dateutil.tz.tzutc())


@pytest.mark.filter_commits
@pytest.mark.parametrize("cached", [False, True], ids=["no cache", "with cache"])
@pytest.mark.parametrize("account, date_to, code",
                         [(3, "2020-02-22", 403), (10, "2020-02-22", 403), (1, "2020-01-12", 200),
                          (1, "2010-01-11", 400), (1, "2020-02-32", 400)])
@skip_if_no_memcached
async def test_filter_commits_bypassing_prs_nasty_input(client, cached, headers, app, client_cache,
                                                        account, date_to, code):
    body = {
        "account": account,
        "date_from": "2020-01-12",
        "date_to": date_to,
        "in": ["{1}"],
        "property": "bypassing_prs",
    }
    response = await client.request(
        method="POST", path="/v1/filter/commits", headers=headers, json=body)
    assert response.status == code


@pytest.mark.filter_releases
async def test_filter_releases_by_tag(client, headers):
    body = {
        "account": 1,
        "date_from": "2018-01-12",
        "date_to": "2020-01-12",
        "timezone": 60,
        "in": ["{1}"],
    }
    response = await client.request(
        method="POST", path="/v1/filter/releases", headers=headers, json=body)
    response_text = (await response.read()).decode("utf-8")
    assert response.status == 200, response_text
    releases = FilteredReleases.from_dict(json.loads(response_text))
    assert len(releases.include.users) == 78
    assert "github.com/mcuadros" in releases.include.users
    assert len(releases.include.jira) == 41
    with_labels = 0
    with_epics = 0
    for key, val in releases.include.jira.items():
        assert key.startswith("DEV-")
        assert key == val.id
        assert val.title
        assert val.type
        with_labels += bool(val.labels)
        with_epics += bool(val.epic)
    assert with_labels == 40
    assert with_epics == 3
    assert len(releases.data) == 21
    pr_numbers = set()
    jira_stats = defaultdict(int)
    for release in releases.data:
        assert release.publisher.startswith("github.com/"), str(release)
        assert len(release.commit_authors) > 0, str(release)
        assert all(a.startswith("github.com/") for a in release.commit_authors), str(release)
        for a in release.commit_authors:
            assert a in releases.include.users
        assert release.commits > 0, str(release)
        assert release.url.startswith("http"), str(release)
        assert release.name, str(release)
        assert release.added_lines > 0, str(release)
        assert release.deleted_lines > 0, str(release)
        assert release.age > timedelta(0), str(release)
        assert release.published >= datetime(year=2018, month=1, day=12, tzinfo=timezone.utc), \
            str(release)
        assert release.repository.startswith("github.com/"), str(release)
        assert len(release.prs) > 0
        for pr in release.prs:
            assert pr.number > 0
            assert pr.number not in pr_numbers
            pr_numbers.add(pr.number)
            assert pr.title
            assert pr.additions + pr.deletions > 0 or pr.number in {804}
            assert (pr.author is None and pr.number in {749, 1203}) \
                or pr.author.startswith("github.com/")
            if pr.jira is not None:
                jira_stats[len(pr.jira)] += 1
    assert jira_stats == {1: 44}


@pytest.mark.filter_releases
async def test_filter_releases_by_branch_no_jira(client, headers, client_cache, app, sdb, mdb):
    app._cache = client_cache
    backup = await mdb.fetch_all(select([Release]))
    backup = [dict(r) for r in backup]
    await sdb.execute(delete(AccountJiraInstallation))
    await mdb.execute(delete(Release))
    try:
        body = {
            "account": 1,
            "date_from": "2018-01-01",
            "date_to": "2020-10-22",
            "in": ["{1}"],
        }
        response = await client.request(
            method="POST", path="/v1/filter/releases", headers=headers, json=body)
        response_text = (await response.read()).decode("utf-8")
        assert response.status == 200, response_text
        releases = FilteredReleases.from_dict(json.loads(response_text))
        assert len(releases.data) == 188
    finally:
        await mdb.execute(insert(Release).values(backup))


@pytest.mark.filter_releases
async def test_filter_releases_by_participants(client, headers):
    body = {
        "account": 1,
        "date_from": "2018-01-12",
        "date_to": "2020-01-12",
        "timezone": 60,
        "in": ["{1}"],
        "with": {"releaser": ["github.com/smola"],
                 "pr_author": ["github.com/mcuadros"],
                 "commit_author": ["github.com/smola"]},
    }
    response = await client.request(
        method="POST", path="/v1/filter/releases", headers=headers, json=body)
    response_text = (await response.read()).decode("utf-8")
    assert response.status == 200, response_text
    releases = FilteredReleases.from_dict(json.loads(response_text))
    releases.include.users = set(releases.include.users)
    assert len(releases.include.users) == 78
    assert "github.com/mcuadros" in releases.include.users
    assert len(releases.data) == 12
    for release in releases.data:
        match_releaser = release.publisher == "github.com/smola"
        match_pr_author = "github.com/mcuadros" in {pr.author for pr in release.prs}
        match_commit_author = "github.com/smola" in release.commit_authors
        assert match_releaser or match_pr_author or match_commit_author, release


@pytest.mark.filter_releases
@pytest.mark.parametrize("account, date_to, code",
                         [(3, "2020-02-22", 403), (10, "2020-02-22", 403), (1, "2020-01-12", 200),
                          (1, "2010-01-11", 400), (1, "2020-02-32", 400)])
async def test_filter_releases_nasty_input(client, headers, account, date_to, code):
    body = {
        "account": account,
        "date_from": "2020-01-12",
        "date_to": date_to,
        "in": ["{1}"],
    }
    response = await client.request(
        method="POST", path="/v1/filter/releases", headers=headers, json=body)
    assert response.status == code


@pytest.mark.filter_releases
async def test_filter_releases_by_jira(client, headers):
    body = {
        "account": 1,
        "date_from": "2018-01-01",
        "date_to": "2020-10-22",
        "in": ["{1}"],
        "jira": {
            "labels_include": ["Bug", "onBoarding", "Performance"],
        },
    }
    response = await client.request(
        method="POST", path="/v1/filter/releases", headers=headers, json=body)
    response_text = (await response.read()).decode("utf-8")
    assert response.status == 200, response_text
    releases = FilteredReleases.from_dict(json.loads(response_text))
    assert len(releases.data) == 8


async def test_get_prs_smoke(client, headers):
    body = {
        "account": 1,
        "prs": [
            {
                "repository": "github.com/src-d/go-git",
                "numbers": list(range(1000, 1100)),
            },
        ],
    }
    response = await client.request(
        method="POST", path="/v1/get/pull_requests", headers=headers, json=body)
    response_body = json.loads((await response.read()).decode("utf-8"))
    assert response.status == 200, response_body
    model = PullRequestSet.from_dict(response_body)
    assert len(model.data) == 51


@pytest.mark.parametrize("account, repo, numbers, status",
                         [(1, "bitbucket.org/whatever", [1, 2, 3], 400),
                          (3, "github.com/src-d/go-git", [1, 2, 3], 422),
                          (4, "github.com/src-d/go-git", [1, 2, 3], 404),
                          (1, "github.com/whatever/else", [1, 2, 3], 403)])
async def test_get_prs_nasty_input(client, headers, account, repo, numbers, status):
    body = {
        "account": account,
        "prs": [
            {
                "repository": repo,
                "numbers": numbers,
            },
        ],
    }
    response = await client.request(
        method="POST", path="/v1/get/pull_requests", headers=headers, json=body)
    response_body = json.loads((await response.read()).decode("utf-8"))
    assert response.status == status, response_body


@pytest.mark.filter_labels
async def test_filter_labels_smoke(client, headers):
    body = {
        "account": 1,
        "repositories": ["{1}"],
    }
    response = await client.request(
        method="POST", path="/v1/filter/labels", headers=headers, json=body)
    response_body = json.loads((await response.read()).decode("utf-8"))
    assert response.status == 200, response_body
    labels = [FilteredLabel.from_dict(i) for i in response_body]
    assert all(labels[i - 1].used_prs >= labels[i].used_prs for i in range(1, len(labels)))
    assert len(labels) == 7
    assert labels[0].name == "enhancement"
    assert labels[0].color == "84b6eb"
    assert labels[0].used_prs == 7


@pytest.mark.filter_labels
@pytest.mark.parametrize("account, repos, status",
                         [(1, ["github.com/whatever/else"], 403),
                          (3, ["github.com/src-d/go-git"], 403),
                          (4, ["github.com/src-d/go-git"], 403),
                          (1, [], 200)])
async def test_filter_labels_nasty_input(client, headers, account, repos, status):
    body = {
        "account": account,
        "repositories": repos,
    }
    response = await client.request(
        method="POST", path="/v1/filter/labels", headers=headers, json=body)
    response_body = json.loads((await response.read()).decode("utf-8"))
    assert response.status == status, response_body
