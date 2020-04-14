from datetime import date, timedelta
from typing import List

from athenian.api.controllers.features.github.pull_request_filter import PullRequestListMiner
from athenian.api.controllers.miners.pull_request_list_item import ParticipationKind, Property, \
    PullRequestListItem


async def test_pr_list_miner_none(mdb, release_match_setting_tag):
    miner = await PullRequestListMiner.mine(
        date.today() - timedelta(days=10 * 365),
        date.today(),
        ["src-d/go-git"],
        release_match_setting_tag,
        [],
        mdb,
        None,
    )
    prs = list(miner)
    assert not prs


async def test_pr_list_miner_match_participants(mdb, release_match_setting_tag):
    miner = await PullRequestListMiner.mine(
        date.today() - timedelta(days=10 * 365),
        date.today(),
        ["src-d/go-git"],
        release_match_setting_tag,
        [],
        mdb,
        None,
    )
    miner.properties = set(Property)
    miner.participants = {ParticipationKind.AUTHOR: ["github.com/mcuadros", "github.com/smola"],
                          ParticipationKind.COMMENTER: ["github.com/mcuadros"]}
    prs = list(miner)  # type: List[PullRequestListItem]
    assert prs
    for pr in prs:
        mcuadros_is_author = "github.com/mcuadros" in pr.participants[ParticipationKind.AUTHOR]
        smola_is_author = "github.com/smola" in pr.participants[ParticipationKind.AUTHOR]
        mcuadros_is_only_commenter = (
            ("github.com/mcuadros" in pr.participants[ParticipationKind.COMMENTER])
            and  # noqa
            (not mcuadros_is_author)
            and  # noqa
            (not smola_is_author)
        )
        assert mcuadros_is_author or smola_is_author or mcuadros_is_only_commenter


async def test_pr_list_miner_no_participants(mdb, release_match_setting_tag):
    miner = await PullRequestListMiner.mine(
        date.today() - timedelta(days=10 * 365),
        date.today(),
        ["src-d/go-git"],
        release_match_setting_tag,
        [],
        mdb,
        None,
    )
    miner.properties = set(Property)
    prs = list(miner)
    assert prs