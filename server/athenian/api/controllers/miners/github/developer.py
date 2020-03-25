from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
import pickle
from typing import Collection, Dict, List, Optional, Sequence, Set, Union

import aiomcache
import databases
import pandas as pd
from sqlalchemy import and_, func, select

from athenian.api.async_read_sql_query import read_sql_query
from athenian.api.cache import cached
from athenian.api.controllers.miners.github.pull_request import ReviewResolution
from athenian.api.models.metadata.github import PullRequest, PullRequestComment, \
    PullRequestReview, PullRequestReviewComment, PushCommit, Release


class DeveloperTopic(Enum):
    """Possible developer statistics kinds."""

    commits_pushed = "dev-commits-pushed"
    lines_changed = "dev-lines-changed"
    prs_created = "dev-prs-created"
    prs_merged = "dev-prs-merged"
    releases = "dev-releases"
    reviews = "dev-reviews"
    review_approvals = "dev-review-approvals"
    review_rejections = "dev-review-rejections"
    review_neutrals = "dev-review-neutrals"
    pr_comments = "dev-pr-comments"
    regular_pr_comments = "dev-regular-pr-comments"
    review_pr_comments = "dev-review-pr-comments"


@dataclass(frozen=True)
class DeveloperStats:
    """Calculated statistics about developer activities."""

    commits_pushed: Optional[int] = None
    lines_changed: Optional[int] = None
    prs_created: Optional[int] = None
    prs_merged: Optional[int] = None
    releases: Optional[int] = None
    reviews: Optional[int] = None
    review_approvals: Optional[int] = None
    review_rejections: Optional[int] = None
    review_neutrals: Optional[int] = None
    pr_comments: Optional[int] = None
    regular_pr_comments: Optional[int] = None
    review_pr_comments: Optional[int] = None


async def _set_commits(stats_by_dev: Dict[str, Dict[str, Union[int, float]]],
                       topics: Set[str],
                       devs: Sequence[str],
                       repos: Collection[str],
                       date_from: datetime,
                       date_to: datetime,
                       conn: databases.core.Connection,
                       cache: Optional[aiomcache.Client]) -> None:
    commits = await _fetch_developer_commits(devs, repos, date_from, date_to, conn, cache)
    commits_by_dev = commits.groupby(PushCommit.author_login.key, sort=False)
    if DeveloperTopic.commits_pushed in topics:
        topic = DeveloperTopic.commits_pushed.name
        for dev, dev_commits in commits_by_dev.count()[PushCommit.additions.key].items():
            stats_by_dev[dev][topic] = dev_commits
    if DeveloperTopic.lines_changed in topics:
        ads = commits_by_dev.sum()
        lines_by_dev = ads[PushCommit.additions.key] + ads[PushCommit.deletions.key]
        topic = DeveloperTopic.lines_changed.name
        for dev, dev_lines in lines_by_dev.items():
            stats_by_dev[dev][topic] = dev_lines


async def _set_prs_created(stats_by_dev: Dict[str, Dict[str, Union[int, float]]],
                           topics: Set[str],
                           devs: Sequence[str],
                           repos: Collection[str],
                           date_from: datetime,
                           date_to: datetime,
                           conn: databases.core.Connection,
                           cache: Optional[aiomcache.Client]) -> None:
    prs = await _fetch_developer_created_prs(devs, repos, date_from, date_to, conn, cache)
    topic = DeveloperTopic.prs_created.name
    for dev, n in prs["created_count"].items():
        stats_by_dev[dev][topic] = n


async def _set_prs_merged(stats_by_dev: Dict[str, Dict[str, Union[int, float]]],
                          topics: Set[str],
                          devs: Sequence[str],
                          repos: Collection[str],
                          date_from: datetime,
                          date_to: datetime,
                          conn: databases.core.Connection,
                          cache: Optional[aiomcache.Client]) -> None:
    prs = await _fetch_developer_merged_prs(devs, repos, date_from, date_to, conn, cache)
    topic = DeveloperTopic.prs_merged.name
    for dev, n in prs["merged_count"].items():
        stats_by_dev[dev][topic] = n


async def _set_releases(stats_by_dev: Dict[str, Dict[str, Union[int, float]]],
                        topics: Set[str],
                        devs: Sequence[str],
                        repos: Collection[str],
                        date_from: datetime,
                        date_to: datetime,
                        conn: databases.core.Connection,
                        cache: Optional[aiomcache.Client]) -> None:
    prs = await _fetch_developer_released_prs(devs, repos, date_from, date_to, conn, cache)
    topic = DeveloperTopic.releases.name
    for dev, n in prs["released_count"].items():
        stats_by_dev[dev][topic] = n


async def _set_reviews(stats_by_dev: Dict[str, Dict[str, Union[int, float]]],
                       topics: Set[str],
                       devs: Sequence[str],
                       repos: Collection[str],
                       date_from: datetime,
                       date_to: datetime,
                       conn: databases.core.Connection,
                       cache: Optional[aiomcache.Client]) -> None:
    reviews = await _fetch_developer_reviews(devs, repos, date_from, date_to, conn, cache)
    if reviews.empty:
        return
    if DeveloperTopic.reviews in topics:
        topic = DeveloperTopic.reviews.name
        for dev, n in (reviews
                       .reset_index()
                       .groupby(PullRequestReview.user_login.key, sort=False)
                       .sum()["reviews_count"]).items():
            stats_by_dev[dev][topic] = n
    if DeveloperTopic.review_approvals in topics:
        topic = DeveloperTopic.review_approvals.name
        for dev, n in reviews.xs(ReviewResolution.APPROVED.value,
                                 level=PullRequestReview.state.key)["reviews_count"].items():
            stats_by_dev[dev][topic] = n
    if DeveloperTopic.review_neutrals in topics:
        topic = DeveloperTopic.review_neutrals.name
        for dev, n in reviews.xs(ReviewResolution.COMMENTED.value,
                                 level=PullRequestReview.state.key)["reviews_count"].items():
            stats_by_dev[dev][topic] = n
    if DeveloperTopic.review_rejections in topics:
        topic = DeveloperTopic.review_rejections.name
        for dev, n in reviews.xs(ReviewResolution.CHANGES_REQUESTED.value,
                                 level=PullRequestReview.state.key)["reviews_count"].items():
            stats_by_dev[dev][topic] = n


async def _set_pr_comments(stats_by_dev: Dict[str, Dict[str, Union[int, float]]],
                           topics: Set[str],
                           devs: Sequence[str],
                           repos: Collection[str],
                           date_from: datetime,
                           date_to: datetime,
                           conn: databases.core.Connection,
                           cache: Optional[aiomcache.Client]) -> None:
    if DeveloperTopic.review_pr_comments in topics or DeveloperTopic.pr_comments in topics:
        review_comments = await _fetch_developer_review_comments(
            devs, repos, date_from, date_to, conn, cache)
        if DeveloperTopic.review_pr_comments in topics:
            topic = DeveloperTopic.review_pr_comments.name
            for dev, n in review_comments["comments_count"].items():
                stats_by_dev[dev][topic] = n
    if DeveloperTopic.regular_pr_comments in topics or DeveloperTopic.pr_comments in topics:
        regular_pr_comments = await _fetch_developer_regular_pr_comments(
            devs, repos, date_from, date_to, conn, cache)
        if DeveloperTopic.regular_pr_comments in topics:
            topic = DeveloperTopic.regular_pr_comments.name
            for dev, n in regular_pr_comments["comments_count"].items():
                stats_by_dev[dev][topic] = n
    if DeveloperTopic.pr_comments in topics:
        topic = DeveloperTopic.pr_comments.name
        for dev, n in (review_comments["comments_count"] +  # noqa: W504
                       regular_pr_comments["comments_count"]).items():
            stats_by_dev[dev][topic] = n


processors = [
    ({DeveloperTopic.commits_pushed, DeveloperTopic.lines_changed}, _set_commits),
    ({DeveloperTopic.prs_created}, _set_prs_created),
    ({DeveloperTopic.prs_merged}, _set_prs_merged),
    ({DeveloperTopic.releases}, _set_releases),
    ({DeveloperTopic.reviews, DeveloperTopic.review_approvals, DeveloperTopic.review_neutrals,
     DeveloperTopic.review_rejections}, _set_reviews),
    ({DeveloperTopic.pr_comments, DeveloperTopic.regular_pr_comments,
     DeveloperTopic.review_pr_comments}, _set_pr_comments),
]


async def calc_developer_metrics(devs: Sequence[str],
                                 repos: Collection[str],
                                 topics: Set[DeveloperTopic],
                                 date_from: date,
                                 date_to: date,
                                 db: databases.Database,
                                 cache: Optional[aiomcache.Client],
                                 ) -> List[DeveloperStats]:
    """Calculate various statistics about developer activities.

    :return: List with calculated stats, the order matches `devs`.
    """
    stats_by_dev = defaultdict(dict)
    date_from = pd.Timestamp(date_from, tzinfo=timezone.utc)
    date_to = pd.Timestamp(date_to, tzinfo=timezone.utc) + timedelta(days=1)
    async with db.connection() as conn:
        for key, setter in processors:
            if key.intersection(topics):
                await setter(stats_by_dev, topics, devs, repos, date_from, date_to, conn, cache)
    return [DeveloperStats(**stats_by_dev[dev]) for dev in devs]


@cached(
    exptime=5 * 60,
    serialize=pickle.dumps,
    deserialize=pickle.loads,
    key=lambda devs, repos, date_from, date_to, **_: (
        ",".join(devs), ",".join(sorted(repos)), date_from.toordinal(), date_to.toordinal()),
)
async def _fetch_developer_commits(devs: Sequence[str],
                                   repos: Collection[str],
                                   date_from: datetime,
                                   date_to: datetime,
                                   db: databases.core.Connection,
                                   cache: Optional[aiomcache.Client],
                                   ) -> pd.DataFrame:
    columns = [PushCommit.additions, PushCommit.deletions, PushCommit.author_login]
    return await read_sql_query(
        select(columns).where(and_(
            PushCommit.committed_date.between(date_from, date_to),
            PushCommit.author_login.in_(devs),
            PushCommit.repository_full_name.in_(repos),
        )),
        db, columns)


@cached(
    exptime=5 * 60,
    serialize=pickle.dumps,
    deserialize=pickle.loads,
    key=lambda devs, repos, date_from, date_to, **_: (
        ",".join(devs), ",".join(sorted(repos)), date_from.toordinal(), date_to.toordinal()),
)
async def _fetch_developer_created_prs(devs: Sequence[str],
                                       repos: Collection[str],
                                       date_from: datetime,
                                       date_to: datetime,
                                       db: databases.core.Connection,
                                       cache: Optional[aiomcache.Client],
                                       ) -> pd.DataFrame:
    return await read_sql_query(
        select([PullRequest.user_login, func.count(PullRequest.created_at)]).where(and_(
            PullRequest.created_at.between(date_from, date_to),
            PullRequest.user_login.in_(devs),
            PullRequest.repository_full_name.in_(repos),
        )).group_by(PullRequest.user_login),
        db, [PullRequest.user_login.key, "created_count"],
        index=PullRequest.user_login.key)


@cached(
    exptime=5 * 60,
    serialize=pickle.dumps,
    deserialize=pickle.loads,
    key=lambda devs, repos, date_from, date_to, **_: (
        ",".join(devs), ",".join(sorted(repos)), date_from.toordinal(), date_to.toordinal()),
)
async def _fetch_developer_merged_prs(devs: Sequence[str],
                                      repos: Collection[str],
                                      date_from: datetime,
                                      date_to: datetime,
                                      db: databases.core.Connection,
                                      cache: Optional[aiomcache.Client],
                                      ) -> pd.DataFrame:
    return await read_sql_query(
        select([PullRequest.merged_by_login, func.count(PullRequest.merged_at)]).where(and_(
            PullRequest.merged_at.between(date_from, date_to),
            PullRequest.merged_by_login.in_(devs),
            PullRequest.repository_full_name.in_(repos),
        )).group_by(PullRequest.merged_by_login),
        db, [PullRequest.merged_by_login.key, "merged_count"],
        index=PullRequest.merged_by_login.key)


@cached(
    exptime=5 * 60,
    serialize=pickle.dumps,
    deserialize=pickle.loads,
    key=lambda devs, repos, date_from, date_to, **_: (
        ",".join(devs), ",".join(sorted(repos)), date_from.toordinal(), date_to.toordinal()),
)
async def _fetch_developer_released_prs(devs: Sequence[str],
                                        repos: Collection[str],
                                        date_from: datetime,
                                        date_to: datetime,
                                        db: databases.core.Connection,
                                        cache: Optional[aiomcache.Client],
                                        ) -> pd.DataFrame:
    return await read_sql_query(
        select([Release.author, func.count(Release.published_at)]).where(and_(
            Release.published_at.between(date_from, date_to),
            Release.author.in_(devs),
            Release.repository_full_name.in_(repos),
        )).group_by(Release.author),
        db, [Release.author.key, "released_count"], index=Release.author.key)


@cached(
    exptime=5 * 60,
    serialize=pickle.dumps,
    deserialize=pickle.loads,
    key=lambda devs, repos, date_from, date_to, **_: (
        ",".join(devs), ",".join(sorted(repos)), date_from.toordinal(), date_to.toordinal()),
)
async def _fetch_developer_reviews(devs: Sequence[str],
                                   repos: Collection[str],
                                   date_from: datetime,
                                   date_to: datetime,
                                   db: databases.core.Connection,
                                   cache: Optional[aiomcache.Client],
                                   ) -> pd.DataFrame:
    return await read_sql_query(
        select([PullRequestReview.user_login, PullRequestReview.state,
                func.count(PullRequestReview.submitted_at)])
        .where(and_(
            PullRequestReview.submitted_at.between(date_from, date_to),
            PullRequestReview.user_login.in_(devs),
            PullRequestReview.repository_full_name.in_(repos),
        )).group_by(PullRequestReview.user_login, PullRequestReview.state),
        db, [PullRequestReview.user_login.key, PullRequestReview.state.key, "reviews_count"],
        index=[PullRequestReview.user_login.key, PullRequestReview.state.key])


@cached(
    exptime=5 * 60,
    serialize=pickle.dumps,
    deserialize=pickle.loads,
    key=lambda devs, repos, date_from, date_to, **_: (
        ",".join(devs), ",".join(sorted(repos)), date_from.toordinal(), date_to.toordinal()),
)
async def _fetch_developer_review_comments(devs: Sequence[str],
                                           repos: Collection[str],
                                           date_from: datetime,
                                           date_to: datetime,
                                           db: databases.core.Connection,
                                           cache: Optional[aiomcache.Client],
                                           ) -> pd.DataFrame:
    return await read_sql_query(
        select([PullRequestReviewComment.user_login,
                func.count(PullRequestReviewComment.created_at)])
        .where(and_(
            PullRequestReviewComment.created_at.between(date_from, date_to),
            PullRequestReviewComment.user_login.in_(devs),
            PullRequestReviewComment.repository_full_name.in_(repos),
        )).group_by(PullRequestReviewComment.user_login),
        db, [PullRequestReviewComment.user_login.key, "comments_count"],
        index=PullRequestReviewComment.user_login.key)


@cached(
    exptime=5 * 60,
    serialize=pickle.dumps,
    deserialize=pickle.loads,
    key=lambda devs, repos, date_from, date_to, **_: (
        ",".join(devs), ",".join(sorted(repos)), date_from.toordinal(), date_to.toordinal()),
)
async def _fetch_developer_regular_pr_comments(devs: Sequence[str],
                                               repos: Collection[str],
                                               date_from: datetime,
                                               date_to: datetime,
                                               db: databases.core.Connection,
                                               cache: Optional[aiomcache.Client],
                                               ) -> pd.DataFrame:
    return await read_sql_query(
        select([PullRequestComment.user_login,
                func.count(PullRequestComment.created_at)])
        .where(and_(
            PullRequestComment.created_at.between(date_from, date_to),
            PullRequestComment.user_login.in_(devs),
            PullRequestComment.repository_full_name.in_(repos),
        )).group_by(PullRequestComment.user_login),
        db, [PullRequestComment.user_login.key, "comments_count"],
        index=PullRequestComment.user_login.key)
