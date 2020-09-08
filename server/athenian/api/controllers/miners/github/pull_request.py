import asyncio
from dataclasses import dataclass, fields as dataclass_fields
from datetime import date, datetime, timezone
from enum import Enum
from itertools import chain
import logging
import pickle
from typing import Collection, Dict, Generator, Iterator, List, Optional, Sequence, Set, Tuple, \
    Union

import aiomcache
import databases
import numpy as np
import pandas as pd
from pandas.core.common import flatten
import sentry_sdk
from sqlalchemy import sql
from sqlalchemy.orm import aliased
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import BinaryExpression

from athenian.api import metadata
from athenian.api.async_read_sql_query import read_sql_query
from athenian.api.cache import cached, CancelCache
from athenian.api.controllers.miners.filters import JIRAFilter, LabelFilter
from athenian.api.controllers.miners.github.precomputed_prs import \
    discover_unreleased_prs, load_inactive_merged_unreleased_prs, load_open_pull_request_facts
from athenian.api.controllers.miners.github.release import map_prs_to_releases, \
    map_releases_to_prs
from athenian.api.controllers.miners.github.released_pr import matched_by_column
from athenian.api.controllers.miners.types import dtmax, dtmin, Fallback, MinedPullRequest, \
    Participants, ParticipationKind, PullRequestFacts
from athenian.api.controllers.settings import ReleaseMatch, ReleaseMatchSetting
from athenian.api.models.metadata.github import Base, NodePullRequestJiraIssues, PullRequest, \
    PullRequestComment, PullRequestCommit, PullRequestLabel, PullRequestReview, \
    PullRequestReviewComment, PullRequestReviewRequest, Release
from athenian.api.models.metadata.jira import Component, Issue
from athenian.api.tracing import sentry_span


@dataclass
class PRDataFrames:
    """Set of dataframes with all the PR data we can reach."""

    prs: pd.DataFrame
    releases: pd.DataFrame
    commits: pd.DataFrame
    jiras: pd.DataFrame
    reviews: pd.DataFrame
    review_comments: pd.DataFrame
    review_requests: pd.DataFrame
    comments: pd.DataFrame
    labels: pd.DataFrame

    def __iter__(self) -> Iterator[pd.DataFrame]:
        """Iterate the contained dataframes."""
        return iter(getattr(self, f.name) for f in dataclass_fields(self))


class PullRequestMiner:
    """Load all the information related to Pull Requests from the metadata DB. Iterate over it \
    by individual PR objects."""

    CACHE_TTL = 5 * 60
    log = logging.getLogger("%s.PullRequestMiner" % metadata.__package__)

    def __init__(self, dfs: PRDataFrames):
        """Initialize a new instance of `PullRequestMiner`."""
        self._dfs = dfs

    def drop(self, node_ids: Collection[str]) -> pd.Index:
        """
        Remove PRs from the given collection of PR node IDs in-place.

        Node IDs don't have to be all present.

        :return: Actually removed node IDs.
        """
        removed = self._dfs.prs.index.intersection(node_ids)
        if removed.empty:
            return removed
        self._dfs.prs.drop(removed, inplace=True)
        for df in self._dfs:
            df.drop(removed, inplace=True, errors="ignore",
                    level=0 if isinstance(df.index, pd.MultiIndex) else None)
        return removed

    @sentry_span
    def _postprocess_cached_prs(
            result: Tuple[PRDataFrames,
                          Dict[str, PullRequestFacts],
                          Set[str],
                          Participants,
                          LabelFilter,
                          JIRAFilter,
                          Dict[str, ReleaseMatch]],
            date_to: date,
            repositories: Set[str],
            participants: Participants,
            labels: LabelFilter,
            jira: JIRAFilter,
            pr_blacklist: Optional[Collection[str]],
            truncate: bool,
            **_) -> Tuple[PRDataFrames, Dict[str, PullRequestFacts],
                          Set[str], Participants, LabelFilter, JIRAFilter,
                          Dict[str, ReleaseMatch]]:
        dfs, _, cached_repositories, cached_participants, cached_labels, cached_jira, _ = result
        cls = PullRequestMiner
        if (repositories - cached_repositories or
                not cls._check_participants_compatibility(cached_participants, participants) or
                not cached_labels.compatible_with(labels) or
                not cached_jira.compatible_with(jira)):
            raise CancelCache()
        to_remove = set()
        if pr_blacklist:
            to_remove.update(pr_blacklist)
        to_remove.update(dfs.prs.index.take(np.where(
            np.in1d(dfs.prs[PullRequest.repository_full_name.key].values,
                    list(repositories), assume_unique=True, invert=True),
        )[0]))
        time_to = None if truncate else pd.Timestamp(date_to, tzinfo=timezone.utc)
        to_remove.update(cls._find_drop_by_participants(dfs, participants, time_to))
        to_remove.update(cls._find_drop_by_labels(dfs, labels))
        to_remove.update(cls._find_drop_by_jira(dfs, jira))
        cls._drop(dfs, to_remove)
        return result

    @classmethod
    @sentry_span
    @cached(
        exptime=lambda cls, **_: cls.CACHE_TTL,
        serialize=pickle.dumps,
        deserialize=pickle.loads,
        key=lambda date_from, date_to, exclude_inactive, release_settings, limit, pr_blacklist, truncate, **_: (  # noqa
            date_from.toordinal(), date_to.toordinal(), exclude_inactive, release_settings,
            limit, ",".join(sorted(pr_blacklist) if pr_blacklist is not None else []), truncate,
        ),
        postprocess=_postprocess_cached_prs,
    )
    async def _mine(cls,
                    date_from: date,
                    date_to: date,
                    repositories: Set[str],
                    participants: Participants,
                    labels: LabelFilter,
                    jira: JIRAFilter,
                    branches: pd.DataFrame,
                    default_branches: Dict[str, str],
                    exclude_inactive: bool,
                    release_settings: Dict[str, ReleaseMatchSetting],
                    limit: int,
                    mdb: databases.Database,
                    pdb: databases.Database,
                    cache: Optional[aiomcache.Client],
                    pr_blacklist: Optional[Collection[str]],
                    truncate: bool,
                    ) -> Tuple[PRDataFrames,
                               Dict[str, PullRequestFacts],
                               Set[str],
                               Participants,
                               LabelFilter,
                               JIRAFilter,
                               Dict[str, ReleaseMatch]]:
        assert isinstance(date_from, date) and not isinstance(date_from, datetime)
        assert isinstance(date_to, date) and not isinstance(date_to, datetime)
        assert isinstance(repositories, set)
        time_from, time_to = (pd.Timestamp(t, tzinfo=timezone.utc) for t in (date_from, date_to))
        if pr_blacklist is not None:
            if len(pr_blacklist) > 0:
                pr_blacklist = PullRequest.node_id.notin_any_values(pr_blacklist)
            else:
                pr_blacklist = None
        # the heaviest task should always go first
        tasks = [
            map_releases_to_prs(
                repositories, branches, default_branches, time_from, time_to,
                participants.get(ParticipationKind.AUTHOR, []),
                participants.get(ParticipationKind.MERGER, []),
                release_settings, limit, mdb, pdb, cache, pr_blacklist, truncate),
            cls._fetch_prs(
                time_from, time_to, repositories, participants, jira, limit, pr_blacklist, mdb),
        ]
        if not exclude_inactive:
            tasks.append(load_inactive_merged_unreleased_prs(
                time_from, time_to, repositories, participants, labels, default_branches,
                release_settings, mdb, pdb, cache))
        else:
            async def dummy_unreleased():
                return pd.DataFrame()
            tasks.append(dummy_unreleased())
        released, prs, unreleased = await asyncio.gather(*tasks, return_exceptions=True)
        for r in (prs, released, unreleased):
            if isinstance(r, Exception):
                raise r from None
        released_prs, releases, matched_bys, dags = released
        if jira:
            extra_prs = pd.concat([released_prs, unreleased], copy=False)
            extra_prs = await cls._filter_jira(extra_prs, jira, mdb)
            prs = pd.concat([prs, extra_prs], copy=False)
        else:
            prs = pd.concat([prs, released_prs, unreleased], copy=False)
        prs = prs[~prs.index.duplicated()]
        if 0 < limit < len(prs):
            prs = prs.take(np.argpartition(
                prs[PullRequest.updated_at.key].values, len(prs) - limit)[len(prs) - limit:])
        prs.sort_index(level=0, inplace=True, sort_remaining=False)
        if truncate:
            cls._truncate_timestamps(prs, time_to)

        tasks = [
            # bypass the useless inner caching by calling _mine_by_ids directly
            cls._mine_by_ids(
                prs, unreleased.index, time_to, releases, matched_bys, branches, default_branches,
                dags, release_settings, mdb, pdb, cache, truncate=truncate),
            load_open_pull_request_facts(prs, pdb),
        ]
        with sentry_sdk.start_span(op="PullRequestMiner.mine/external_data"):
            mined, open_facts = await asyncio.gather(*tasks, return_exceptions=True)
            for r in (mined, open_facts):
                if isinstance(r, Exception):
                    raise r from None
        dfs, unreleased_facts = mined

        to_drop = cls._find_drop_by_participants(dfs, participants, None if truncate else time_to)
        to_drop |= cls._find_drop_by_labels(dfs, labels)
        if exclude_inactive:
            to_drop |= cls._find_drop_by_inactive(dfs, time_from, time_to)
        cls._drop(dfs, to_drop)

        facts = open_facts
        for k, v in unreleased_facts.items():  # merged unreleased PR precomputed facts
            if v is not None:  # it can be None because the pdb table is filled in two steps
                facts[k] = v
        # we don't care about the precomputed facts, they are here for the reference

        return dfs, facts, repositories, participants, labels, jira, matched_bys

    _postprocess_cached_prs = staticmethod(_postprocess_cached_prs)

    @classmethod
    @cached(
        exptime=lambda cls, **_: cls.CACHE_TTL,
        serialize=pickle.dumps,
        deserialize=pickle.loads,
        key=lambda prs, unreleased, releases, time_to, truncate=True, **_: (
            ",".join(prs.index), ",".join(unreleased),
            ",".join(releases[Release.id.key].values), time_to.timestamp(),
            truncate,
        ),
        version=2,
    )
    async def mine_by_ids(cls,
                          prs: pd.DataFrame,
                          unreleased: Collection[str],
                          time_to: datetime,
                          releases: pd.DataFrame,
                          matched_bys: Dict[str, ReleaseMatch],
                          branches: pd.DataFrame,
                          default_branches: Dict[str, str],
                          dags: Dict[str, Tuple[np.ndarray, np.ndarray, np.ndarray]],
                          release_settings: Dict[str, ReleaseMatchSetting],
                          mdb: databases.Database,
                          pdb: databases.Database,
                          cache: Optional[aiomcache.Client],
                          truncate: bool = True,
                          ) -> Tuple[PRDataFrames, Dict[str, PullRequestFacts]]:
        """
        Fetch PR metadata for certain PRs.

        :param prs: pandas DataFrame with fetched PullRequest-s. Only the details about those PRs \
                    will be loaded from the DB.
        :param truncate: Do not load anything after `time_to`.
        :return: List of mined DataFrame-s + mapping to pickle-d PullRequestFacts for unreleased \
                 merged PR. Why so complex? Performance.
        """
        return await cls._mine_by_ids(
            prs, unreleased, time_to, releases, matched_bys, branches, default_branches, dags,
            release_settings, mdb, pdb, cache, truncate=truncate)

    @classmethod
    @sentry_span
    async def _mine_by_ids(cls,
                           prs: pd.DataFrame,
                           unreleased: Collection[str],
                           time_to: datetime,
                           releases: pd.DataFrame,
                           matched_bys: Dict[str, ReleaseMatch],
                           branches: pd.DataFrame,
                           default_branches: Dict[str, str],
                           dags: Dict[str, Tuple[np.ndarray, np.ndarray, np.ndarray]],
                           release_settings: Dict[str, ReleaseMatchSetting],
                           mdb: databases.Database,
                           pdb: databases.Database,
                           cache: Optional[aiomcache.Client],
                           truncate: bool = True,
                           ) -> Tuple[PRDataFrames, Dict[str, PullRequestFacts]]:
        node_ids = prs.index if len(prs) > 0 else set()
        facts = {}  # precomputed PullRequestFacts about merged unreleased PRs

        @sentry_span
        async def fetch_reviews():
            return await cls._read_filtered_models(
                mdb, PullRequestReview, node_ids, time_to,
                columns=[PullRequestReview.submitted_at, PullRequestReview.user_id,
                         PullRequestReview.state, PullRequestReview.user_login],
                created_at=truncate)

        @sentry_span
        async def fetch_review_comments():
            return await cls._read_filtered_models(
                mdb, PullRequestReviewComment, node_ids, time_to,
                columns=[PullRequestReviewComment.created_at, PullRequestReviewComment.user_id],
                created_at=truncate)

        @sentry_span
        async def fetch_review_requests():
            return await cls._read_filtered_models(
                mdb, PullRequestReviewRequest, node_ids, time_to,
                columns=[PullRequestReviewRequest.created_at],
                created_at=truncate)

        @sentry_span
        async def fetch_comments():
            return await cls._read_filtered_models(
                mdb, PullRequestComment, node_ids, time_to,
                columns=[PullRequestComment.created_at, PullRequestComment.user_id,
                         PullRequestComment.user_login],
                created_at=truncate)

        @sentry_span
        async def fetch_commits():
            return await cls._read_filtered_models(
                mdb, PullRequestCommit, node_ids, time_to,
                columns=[PullRequestCommit.authored_date, PullRequestCommit.committed_date,
                         PullRequestCommit.author_login, PullRequestCommit.committer_login],
                created_at=truncate)

        @sentry_span
        async def map_releases():
            if truncate:
                merged_mask = prs[PullRequest.merged_at.key] <= time_to
            else:
                merged_mask = ~prs[PullRequest.merged_at.key].isnull()
            merged_mask &= ~prs.index.isin(unreleased)
            merged_prs = prs.take(np.where(merged_mask)[0])
            subtasks = [map_prs_to_releases(
                merged_prs, releases, matched_bys, branches, default_branches, time_to,
                dags, release_settings, mdb, pdb, cache),
                discover_unreleased_prs(
                    prs.take(np.where(~merged_mask)[0]),
                    dtmax(releases[Release.published_at.key].max(), time_to),
                    matched_bys, default_branches, release_settings, pdb)]
            df_facts, other_facts = await asyncio.gather(*subtasks, return_exceptions=True)
            for r in (df_facts, other_facts):
                if isinstance(r, Exception):
                    raise r from None
            nonlocal facts
            df, facts = df_facts
            facts.update(other_facts)
            return df

        @sentry_span
        async def fetch_labels():
            return await cls._read_filtered_models(
                mdb, PullRequestLabel, node_ids, time_to,
                columns=[PullRequestLabel.name, PullRequestLabel.description,
                         PullRequestLabel.color],
                created_at=False)

        @sentry_span
        async def fetch_jira():
            _map = aliased(NodePullRequestJiraIssues, name="m")
            _issue = aliased(Issue, name="i")
            _issue_epic = aliased(Issue, name="e")
            selected = [
                PullRequest.node_id, _issue.key, _issue.title, _issue.type, _issue.status,
                _issue.created, _issue.updated, _issue.resolved, _issue.labels, _issue.components,
                _issue.acc_id, _issue_epic.key.label("epic"),
            ]
            df = await read_sql_query(
                sql.select(selected).select_from(sql.join(
                    PullRequest, sql.join(
                        _map, sql.join(_issue, _issue_epic, sql.and_(
                            _issue.epic_id == _issue_epic.id,
                            _issue.acc_id == _issue_epic.acc_id)),
                        sql.and_(_map.jira_id == _issue.id,
                                 _map.jira_acc == _issue.acc_id)),
                    PullRequest.node_id == _map.node_id,
                )).where(PullRequest.node_id.in_(node_ids)),
                mdb, columns=selected, index=[PullRequest.node_id.key, _issue.key.key])
            if df.empty:
                df.drop([Issue.acc_id.key, Issue.components.key], inplace=True, axis=1)
                return df
            components = df[[Issue.acc_id.key, Issue.components.key]] \
                .groupby(Issue.acc_id.key, sort=False).aggregate(lambda s: set(flatten(s)))
            rows = await mdb.fetch_all(
                sql.select([Component.acc_id, Component.id, Component.name])
                .where(sql.or_(*(sql.and_(Component.id.in_(vals),
                                          Component.acc_id == int(acc))
                                 for acc, vals in zip(components.index.values,
                                                      components[Issue.components.key].values)))))
            cmap = {}
            for r in rows:
                cmap.setdefault(r[0], {})[r[1]] = r[2].lower()
            df[Issue.labels.key] = (
                df[Issue.labels.key].apply(lambda i: [s.lower() for s in i])
                +
                df[[Issue.acc_id.key, Issue.components.key]]
                .apply(lambda row: [cmap[row[Issue.acc_id.key]][c]
                                    for c in row[Issue.components.key]],
                       axis=1)
            )
            df.drop([Issue.acc_id.key, Issue.components.key], inplace=True, axis=1)
            return df

        # the order is important: it provides the best performance
        # we launch coroutines from the heaviest to the lightest
        dfs = await asyncio.gather(
            map_releases(),
            fetch_commits(),
            fetch_jira(),
            fetch_reviews(),
            fetch_review_comments(),
            fetch_review_requests(),
            fetch_comments(),
            fetch_labels(),
            return_exceptions=True)
        for df in dfs:
            if isinstance(df, Exception):
                raise df from None
        return PRDataFrames(prs, *dfs), facts

    @classmethod
    @sentry_span
    async def mine(cls,
                   date_from: date,
                   date_to: date,
                   time_from: datetime,
                   time_to: datetime,
                   repositories: Set[str],
                   participants: Participants,
                   labels: LabelFilter,
                   jira: JIRAFilter,
                   branches: pd.DataFrame,
                   default_branches: Dict[str, str],
                   exclude_inactive: bool,
                   release_settings: Dict[str, ReleaseMatchSetting],
                   mdb: databases.Database,
                   pdb: databases.Database,
                   cache: Optional[aiomcache.Client],
                   limit: int = 0,
                   pr_blacklist: Optional[Collection[str]] = None,
                   truncate: bool = True,
                   ) -> Tuple["PullRequestMiner",
                              Dict[str, PullRequestFacts],
                              Dict[str, ReleaseMatch]]:
        """
        Mine metadata about pull requests according to the numerous filters.

        First returned item: a new `PullRequestMiner` with the PRs satisfying \
                             to the specified filters.
        Second returned item: the precomputed facts about unreleased pull requests. \
                              This is an optimization which breaks the abstraction a bit.
        The third returned item: the `matched_bys` - release matches for each repository.

        :param date_from: Fetch PRs created starting from this date, inclusive.
        :param date_to: Fetch PRs created ending with this date, inclusive.
        :param time_from: Precise timestamp of since when PR events are allowed to happen.
        :param time_to: Precise timestamp of until when PR events are allowed to happen.
        :param repositories: PRs must belong to these repositories (prefix excluded).
        :param participants: PRs must have these user IDs in the specified participation roles \
                             (OR aggregation). An empty dict means everybody.
        :param labels: PRs must be labeled according to this filter's include & exclude sets.
        :param jira: JIRA filters for those PRs that are matched with JIRA issues.
        :param branches: Preloaded DataFrame with branches in the specified repositories.
        :param default_branches: Mapping from repository names to their default branch names.
        :param exclude_inactive: Ors must have at least one event in the given time frame.
        :param release_settings: Release match settings of the account.
        :param limit: Maximum number of PRs to return. The list is sorted by the last update \
                      timestamp. 0 means no limit.
        :param mdb: Metadata db instance.
        :param pdb: Precomputed db instance.
        :param cache: memcached client to cache the collected data.
        :param pr_blacklist: completely ignore the existence of these PR node IDs.
        :param truncate: activate the "time machine" and erase everything after `time_to`.
        """
        date_from_with_time = datetime.combine(date_from, datetime.min.time(), tzinfo=timezone.utc)
        date_to_with_time = datetime.combine(date_to, datetime.min.time(), tzinfo=timezone.utc)
        assert time_from >= date_from_with_time
        assert time_to <= date_to_with_time
        dfs, facts, _, _, _, _, matched_bys = await cls._mine(
            date_from, date_to, repositories, participants, labels, jira, branches,
            default_branches, exclude_inactive, release_settings, limit, mdb, pdb, cache,
            pr_blacklist=pr_blacklist, truncate=truncate)
        cls._truncate_prs(dfs, time_from, time_to)
        if truncate:
            for df in dfs:
                cls._truncate_timestamps(df, time_to)
        return cls(dfs), facts, matched_bys

    @classmethod
    @sentry_span
    async def _fetch_prs(cls,
                         time_from: datetime,
                         time_to: datetime,
                         repositories: Set[str],
                         participants: Participants,
                         jira: JIRAFilter,
                         limit: int,
                         pr_blacklist: Optional[BinaryExpression],
                         mdb: databases.Database) -> pd.DataFrame:
        postgres = mdb.url.dialect in ("postgres", "postgresql")
        filters = [
            sql.or_(PullRequest.closed_at.is_(None), PullRequest.closed_at >= time_from),
            PullRequest.created_at < time_to,
            PullRequest.hidden.is_(False),
            PullRequest.repository_full_name.in_(repositories),
        ]
        if pr_blacklist is not None:
            filters.append(pr_blacklist)
        if len(participants) == 1:
            if ParticipationKind.AUTHOR in participants:
                filters.append(PullRequest.user_login.in_(participants[ParticipationKind.AUTHOR]))
            elif ParticipationKind.MERGER in participants:
                filters.append(
                    PullRequest.merged_by_login.in_(participants[ParticipationKind.MERGER]))
        elif len(participants) == 2 and ParticipationKind.AUTHOR in participants and \
                ParticipationKind.MERGER in participants:
            filters.append(sql.or_(
                PullRequest.user_login.in_(participants[ParticipationKind.AUTHOR]),
                PullRequest.merged_by_login.in_(participants[ParticipationKind.MERGER]),
            ))
        if not jira:
            query = sql.select([PullRequest]).where(sql.and_(*filters))
        else:
            query = await cls._generate_jira_prs_query(filters, jira, postgres, mdb)
        if limit > 0:
            query = query.order_by(sql.desc(PullRequest.updated_at)).limit(limit)
        return await read_sql_query(query, mdb, PullRequest, index=PullRequest.node_id.key)

    @classmethod
    @sentry_span
    async def _filter_jira(cls,
                           prs: pd.DataFrame,
                           jira: JIRAFilter,
                           mdb: databases.Database) -> pd.DataFrame:
        assert jira
        postgres = mdb.url.dialect in ("postgres", "postgresql")
        filters = [PullRequest.node_id.in_(prs.index.values)]
        query = await cls._generate_jira_prs_query(filters, jira, postgres, mdb)
        return await read_sql_query(query, mdb, PullRequest, index=PullRequest.node_id.key)

    @staticmethod
    async def _generate_jira_prs_query(filters: list,
                                       jira: JIRAFilter,
                                       postgres: bool,
                                       mdb: databases.Database) -> sql.Select:
        _map = aliased(NodePullRequestJiraIssues, name="m")
        _issue = aliased(Issue, name="j")
        if jira.labels:
            all_labels = set()
            for label in chain(jira.labels.include, jira.labels.exclude):
                for part in label.split(","):
                    all_labels.add(part.strip())
            rows = await mdb.fetch_all(sql.select([Component.id, Component.name]).where(sql.and_(
                sql.func.lower(Component.name).in_(all_labels),
                Component.acc_id == jira.account,
            )))
            components = {r[1].lower(): r[0] for r in rows}
        if postgres:
            if jira.labels.include:
                singles, multiples = LabelFilter.split(jira.labels.include)
                or_items = []
                if singles:
                    or_items.append(_issue.labels.overlap(singles))
                or_items.extend(_issue.labels.contains(m) for m in multiples)
                if components:
                    if singles:
                        cinc = [components[s] for s in singles if s in components]
                        if cinc:
                            or_items.append(_issue.components.overlap(cinc))
                    if multiples:
                        cinc = [[components[c] for c in g if c in components] for g in multiples]
                        or_items.extend(_issue.components.contains(g) for g in cinc if g)
                filters.append(sql.or_(*or_items))
            if jira.labels.exclude:
                filters.append(sql.not_(_issue.labels.overlap(jira.labels.exclude)))
                if components:
                    filters.append(sql.not_(_issue.components.overlap(
                        [components[s] for s in jira.labels.exclude if s in components])))
        else:
            # neither 100% correct nor efficient, but enough for local development
            if jira.labels.include:
                or_items = []
                singles, multiples = LabelFilter.split(jira.labels.include)
                or_items.extend(_issue.labels.like("%%%s%%" % s) for s in singles)
                or_items.extend(
                    sql.and_(*(_issue.labels.like("%%%s%%" % s) for s in g)) for g in multiples)
                if components:
                    if singles:
                        or_items.extend(
                            _issue.components.like("%%%s%%" % components[s])
                            for s in singles if s in components)
                    if multiples:
                        or_items.extend(
                            sql.and_(*(_issue.components.like("%%%s%%" % components[s]) for s in g
                                       if s in components))
                            for g in multiples)
                filters.append(sql.or_(*or_items))
            if jira.labels.exclude:
                filters.append(sql.not_(sql.or_(*(
                    _issue.labels.like("%%%s%%" % s) for s in jira.labels.exclude))))
                if components:
                    filters.append(sql.not_(sql.or_(*(
                        _issue.components.like("%%%s%%" % components[s])
                        for s in jira.labels.exclude if s in components))))
        if jira.issue_types:
            filters.append(sql.func.lower(_issue.type).in_(jira.issue_types))
        if not jira.epics:
            return sql.select([PullRequest]).select_from(sql.join(
                PullRequest, sql.join(_map, _issue, _map.jira_id == _issue.id),
                PullRequest.node_id == _map.node_id,
            )).where(sql.and_(*filters))
        _issue_epic = aliased(Issue, name="e")
        filters.append(_issue_epic.key.in_(jira.epics))
        return sql.select([PullRequest]).select_from(sql.join(
            PullRequest, sql.join(
                _map, sql.join(_issue, _issue_epic, _issue.epic_id == _issue_epic.id),
                _map.jira_id == _issue.id),
            PullRequest.node_id == _map.node_id,
        )).where(sql.and_(*filters))

    @staticmethod
    def _check_participants_compatibility(cached_participants: Participants,
                                          participants: Participants) -> bool:
        if not cached_participants:
            return True
        if not participants:
            return False
        for k, v in participants.items():
            if v - cached_participants.get(k, set()):
                return False
        return True

    @classmethod
    @sentry_span
    def _remove_spurious_prs(cls, time_from: datetime, dfs: PRDataFrames) -> None:
        old_releases = np.where(dfs.releases[Release.published_at.key] < time_from)[0]
        if len(old_releases) == 0:
            return
        cls._drop(dfs, dfs.releases.index[old_releases])

    @classmethod
    def _drop(cls, dfs: PRDataFrames, pr_ids: Collection[str]) -> None:
        if len(pr_ids) == 0:
            return
        for df in dfs:
            df.drop(pr_ids,
                    level=0 if isinstance(df.index, pd.MultiIndex) else None,
                    inplace=True,
                    errors="ignore")

    @classmethod
    @sentry_span
    def _find_drop_by_participants(cls,
                                   dfs: PRDataFrames,
                                   participants: Participants,
                                   time_to: Optional[datetime],
                                   ) -> pd.Index:
        if not participants:
            return pd.Index([])
        if time_to is not None:
            for df_name, col in (("commits", PullRequestCommit.committed_date),
                                 ("reviews", PullRequestReview.created_at),
                                 ("review_comments", PullRequestReviewComment.created_at),
                                 ("review_requests", PullRequestReviewRequest.created_at),
                                 ("comments", PullRequestComment.created_at)):
                df = getattr(dfs, df_name)
                setattr(dfs, df_name, df.take(np.where(df[col.key] < time_to)[0]))
        passed = []
        dict_iter = (
            (dfs.prs, PullRequest.user_login, None, ParticipationKind.AUTHOR),
            (dfs.prs, PullRequest.merged_by_login, PullRequest.merged_at, ParticipationKind.MERGER),  # noqa
            (dfs.releases, Release.author, Release.published_at, ParticipationKind.RELEASER),
        )
        for df, part_col, date_col, pk in dict_iter:
            col_parts = participants.get(pk)
            if not col_parts:
                continue
            mask = df[part_col.key].isin(col_parts)
            if time_to is not None and date_col is not None:
                mask &= df[date_col.key] < time_to
            passed.append(df.index.take(np.where(mask)[0]))
        reviewers = participants.get(ParticipationKind.REVIEWER)
        if reviewers:
            ulkr = PullRequestReview.user_login.key
            ulkp = PullRequest.user_login.key
            user_logins = pd.merge(dfs.reviews[ulkr].droplevel(1), dfs.prs[ulkp],
                                   left_index=True, right_index=True, how="left", copy=False)
            ulkr += "_x"
            ulkp += "_y"
            passed.append(user_logins.index.take(np.where(
                (user_logins[ulkr] != user_logins[ulkp]) & user_logins[ulkr].isin(reviewers),
            )[0]).unique())
        for df, col, pk in (
                (dfs.comments, PullRequestComment.user_login, ParticipationKind.COMMENTER),
                (dfs.commits, PullRequestCommit.author_login, ParticipationKind.COMMIT_AUTHOR),
                (dfs.commits, PullRequestCommit.committer_login, ParticipationKind.COMMIT_COMMITTER)):  # noqa
            col_parts = participants.get(pk)
            if not col_parts:
                continue
            passed.append(df.index.get_level_values(0).take(np.where(
                df[col.key].isin(col_parts))[0]).unique())
        while len(passed) > 1:
            new_passed = []
            for i in range(0, len(passed), 2):
                if i + 1 < len(passed):
                    new_passed.append(passed[i].union(passed[i + 1]))
                else:
                    new_passed.append(passed[i])
            passed = new_passed
        return dfs.prs.index.difference(passed[0])

    @classmethod
    @sentry_span
    def _find_drop_by_labels(cls, dfs: PRDataFrames, labels: LabelFilter) -> pd.Index:
        if not labels:
            return pd.Index([])
        df_labels_index = dfs.labels.index.get_level_values(0)
        df_labels_names = dfs.labels[PullRequestLabel.name.key].str.lower().values
        left = cls._find_left_by_labels(df_labels_index, df_labels_names, labels)
        return dfs.prs.index.difference(left)

    @classmethod
    def _find_left_by_labels(cls,
                             df_labels_index: pd.Index,
                             df_labels_names: Sequence[str],
                             labels: LabelFilter) -> pd.Index:
        left_include = left_exclude = None
        if labels.include:
            singles, multiples = LabelFilter.split(labels.include)
            left_include = df_labels_index.take(
                np.where(np.in1d(df_labels_names, singles))[0],
            ).unique()
            for group in multiples:
                passed = df_labels_index
                for label in group:
                    passed = passed.intersection(
                        df_labels_index.take(np.where(df_labels_names == label)))
                    if passed.empty:
                        break
                left_include = left_include.union(passed)
        if labels.exclude:
            left_exclude = df_labels_index.difference(df_labels_index.take(
                np.where(np.in1d(df_labels_names, list(labels.exclude)))[0],
            ).unique())
        if labels.include:
            if labels.exclude:
                left = left_include.intersection(left_exclude)
            else:
                left = left_include
        else:
            left = left_exclude
        return left

    @classmethod
    @sentry_span
    def _find_drop_by_jira(cls, dfs: PRDataFrames, jira: JIRAFilter) -> pd.Index:
        if not jira:
            return pd.Index([])
        left = []
        jira_index = dfs.jiras.index.get_level_values(0)
        if jira.labels:
            df_labels_names = dfs.jiras[Issue.labels.key].values
            df_labels_index = pd.Index(np.repeat(jira_index, [len(v) for v in df_labels_names]))
            df_labels_names = list(pd.core.common.flatten(df_labels_names))
            left.append(cls._find_left_by_labels(df_labels_index, df_labels_names, jira.labels))
        if jira.epics:
            left.append(jira_index.take(np.where(
                dfs.jiras["epic"].isin(jira.epics))[0]).unique())
        if jira.issue_types:
            left.append(dfs.jiras.index.get_level_values(0).take(np.where(
                dfs.jiras[Issue.type.key].str.lower().isin(jira.issue_types))[0]).unique())
        result = left[0]
        for other in left[1:]:
            result = result.intersection(other)
        return dfs.prs.index.difference(result)

    @classmethod
    @sentry_span
    def _find_drop_by_inactive(cls,
                               dfs: PRDataFrames,
                               time_from: datetime,
                               time_to: datetime) -> pd.Index:
        activities = [
            dfs.prs[PullRequest.created_at.key],
            dfs.prs[PullRequest.closed_at.key],
            dfs.commits[PullRequestCommit.committed_date.key],
            dfs.review_requests[PullRequestReviewRequest.created_at.key],
            dfs.reviews[PullRequestReview.created_at.key],
            dfs.comments[PullRequestComment.created_at.key],
            dfs.releases[Release.published_at.key],
        ]
        for df in activities:
            if df.index.nlevels > 1:
                df.index = df.index.droplevel(1)
            df.name = "timestamp"
        activities = pd.concat(activities, copy=False)
        active_prs = activities.index.take(np.where(
            activities.between(time_from, time_to))[0]).drop_duplicates()
        inactive_prs = dfs.prs.index.difference(active_prs)
        return inactive_prs

    @staticmethod
    async def _read_filtered_models(conn: Union[databases.core.Connection, databases.Database],
                                    model_cls: Base,
                                    node_ids: Collection[str],
                                    time_to: datetime,
                                    columns: Optional[List[InstrumentedAttribute]] = None,
                                    created_at=True,
                                    ) -> pd.DataFrame:
        if columns is not None:
            columns = [model_cls.pull_request_node_id, model_cls.node_id] + columns
        node_id_filter = model_cls.pull_request_node_id.in_(node_ids)
        if created_at:
            filters = sql.and_(node_id_filter, model_cls.created_at < time_to)
        else:
            filters = node_id_filter
        df = await read_sql_query(
            sql.select(columns or [model_cls]).where(filters),
            con=conn,
            columns=columns or model_cls,
            index=[model_cls.pull_request_node_id.key, model_cls.node_id.key])
        return df

    @classmethod
    @sentry_span
    def _truncate_prs(cls,
                      dfs: PRDataFrames,
                      time_from: datetime,
                      time_to: datetime,
                      ) -> None:
        """
        Remove PRs outside of the given time range.

        This is used to correctly handle timezone offsets.
        """
        # filter out PRs which were released before `time_from`
        unreleased = dfs.releases.index.take(np.where(
            dfs.releases[Release.published_at.key] < time_from)[0])
        # closed but not merged in `[date_from, time_from]`
        unrejected = dfs.prs.index.take(np.where(
            (dfs.prs[PullRequest.closed_at.key] < time_from) &
            dfs.prs[PullRequest.merged_at.key].isnull())[0])
        # created in `[time_to, date_to]`
        uncreated = dfs.prs.index.take(np.where(
            dfs.prs[PullRequest.created_at.key] >= time_to)[0])
        to_remove = unreleased.union(unrejected.union(uncreated))
        cls._drop(dfs, to_remove)

    @staticmethod
    def _truncate_timestamps(df: pd.DataFrame, upto: datetime):
        """Set all the timestamps after `upto` to NaT to avoid "future leakages"."""
        for col in df.select_dtypes(include=[object]):
            try:
                df.loc[df[col] > upto, col] = pd.NaT
            except TypeError:
                continue
        for col in df.select_dtypes(include=["datetime"]):
            df.loc[df[col] > upto, col] = pd.NaT

    def __len__(self) -> int:
        """Return the number of loaded pull requests."""
        return len(self._dfs.prs)

    def __iter__(self) -> Generator[MinedPullRequest, None, None]:
        """Iterate over the individual pull requests."""
        df_fields = [f.name for f in dataclass_fields(MinedPullRequest) if f.name != "pr"]
        dfs = []
        grouped_df_iters = []
        index_backup = []
        for k in df_fields:
            plural = k.endswith("s")
            df = getattr(self._dfs, k if plural else (k + "s"))
            dfs.append(df)
            # our very own groupby() allows us to call take() with reduced overhead
            node_ids = df.index.get_level_values(0).values
            if df.index.nlevels > 1:
                # this is not really required but it makes iteration deterministic
                order_keys = (node_ids + df.index.get_level_values(1).values).astype("U")
                node_ids = node_ids.astype("U")
            else:
                order_keys = node_ids = node_ids.astype("U")
            node_ids_order = np.argsort(order_keys)
            node_ids = node_ids[node_ids_order]
            node_ids_backtrack = np.arange(0, len(df))[node_ids_order]
            node_ids_unique_counts = np.unique(node_ids, return_counts=True)[1]
            node_ids_group_counts = np.zeros(len(node_ids_unique_counts) + 1, dtype=int)
            np.cumsum(node_ids_unique_counts, out=node_ids_group_counts[1:])
            keys = node_ids[node_ids_group_counts[:-1]]
            groups = np.split(node_ids_backtrack, node_ids_group_counts[1:-1])
            grouped_df_iters.append(iter(zip(keys, groups)))
            if plural:
                index_backup.append(df.index)
                df.index = df.index.droplevel(0)
            else:
                index_backup.append(None)
        try:
            grouped_df_states = []
            for i in grouped_df_iters:
                try:
                    grouped_df_states.append(next(i))
                except StopIteration:
                    grouped_df_states.append((None, None))
            empty_df_cache = {}
            pr_columns = [PullRequest.node_id.key]
            pr_columns.extend(self._dfs.prs.columns)
            if not self._dfs.prs.index.is_monotonic_increasing:
                raise IndexError("PRs index must be pre-sorted ascending: "
                                 "prs.sort_index(inplace=True)")
            for pr_tuple in self._dfs.prs.itertuples():
                pr_node_id = pr_tuple.Index
                items = {"pr": dict(zip(pr_columns, pr_tuple))}
                for i, (k, (state_pr_node_id, gdf), git, df) in enumerate(zip(
                        df_fields, grouped_df_states, grouped_df_iters, dfs)):
                    if state_pr_node_id == pr_node_id:
                        if not k.endswith("s"):
                            # much faster than df.iloc[gdf[0]]
                            gdf = {c: v for c, v in zip(df.columns, df._data.fast_xs(gdf[0]))}
                        else:
                            gdf = df.take(gdf)
                        items[k] = gdf
                        try:
                            grouped_df_states[i] = next(git)
                        except StopIteration:
                            grouped_df_states[i] = None, None
                    else:
                        if k.endswith("s"):
                            try:
                                items[k] = empty_df_cache[k]
                            except KeyError:
                                items[k] = empty_df_cache[k] = df.iloc[:0].copy()
                        else:
                            items[k] = {c: None for c in df.columns}
                yield MinedPullRequest(**items)
        finally:
            for df, index in zip(dfs, index_backup):
                if index is not None:
                    df.index = index


class ReviewResolution(Enum):
    """Possible review "state"-s in the metadata DB."""

    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    COMMENTED = "COMMENTED"


class ImpossiblePullRequest(Exception):
    """Raised by PullRequestFactsMiner._compile() on broken PRs."""


class PullRequestFactsMiner:
    """Extract the pull request event timestamps from MinedPullRequest-s."""

    log = logging.getLogger("%s.PullRequestFactsMiner" % metadata.__package__)
    dummy_reviews = pd.Series(["INVALID", pd.NaT],
                              index=[PullRequestReview.state.key,
                                     PullRequestReview.submitted_at.key])

    def __init__(self, bots: Set[str]):
        """Require the set of bots to be preloaded."""
        self._bots = np.sort(list(bots))

    def __call__(self, pr: MinedPullRequest) -> PullRequestFacts:
        """
        Extract the pull request event timestamps from a MinedPullRequest.

        May raise ImpossiblePullRequest if the PR has an "impossible" state like
        created after closed.
        """
        created_at = Fallback(pr.pr[PullRequest.created_at.key], None)
        merged_at = Fallback(pr.pr[PullRequest.merged_at.key], None)
        closed_at = Fallback(pr.pr[PullRequest.closed_at.key], None)
        if merged_at and not closed_at:
            self.log.error("[DEV-508] PR %s (%s#%d) is merged at %s but not closed",
                           pr.pr[PullRequest.node_id.key],
                           pr.pr[PullRequest.repository_full_name.key],
                           pr.pr[PullRequest.number.key],
                           merged_at.best)
            closed_at = merged_at
        # we don't need these indexes
        pr.comments.reset_index(inplace=True, drop=True)
        pr.reviews.reset_index(inplace=True, drop=True)
        first_commit = Fallback(pr.commits[PullRequestCommit.authored_date.key].min(), None)
        # yes, first_commit uses authored_date while last_commit uses committed_date
        last_commit = Fallback(pr.commits[PullRequestCommit.committed_date.key].max(), None)
        # convert to "U" dtype to enable sorting in np.in1d
        authored_comments = pr.comments[PullRequestReviewComment.user_login.key].values.astype("U")
        external_comments_times = pr.comments[PullRequestComment.created_at.key].take(
            np.where((authored_comments != pr.pr[PullRequest.user_login.key]) &
                     np.in1d(authored_comments, self._bots, invert=True))[0])
        first_comment = dtmin(
            pr.review_comments[PullRequestReviewComment.created_at.key].min(),
            pr.reviews[PullRequestReview.submitted_at.key].min(),
            external_comments_times.min())
        if closed_at and first_comment is not None and first_comment > closed_at.best:
            first_comment = None
        first_comment_on_first_review = Fallback(first_comment, merged_at)
        if first_comment_on_first_review:
            committed_dates = pr.commits[PullRequestCommit.committed_date.key]
            last_commit_before_first_review = Fallback(
                committed_dates.take(np.where(
                    committed_dates <= first_comment_on_first_review.best)[0]).max(),
                first_comment_on_first_review)
            # force pushes that were lost
            first_commit = Fallback.min(first_commit, last_commit_before_first_review)
            last_commit = Fallback.max(last_commit, first_commit)
            first_review_request_backup = Fallback.min(
                Fallback.max(created_at, last_commit_before_first_review),
                first_comment_on_first_review)
        else:
            last_commit_before_first_review = Fallback(None, None)
            first_review_request_backup = None
        first_review_request = pr.review_requests[PullRequestReviewRequest.created_at.key].min()
        if first_review_request_backup and first_review_request == first_review_request and \
                first_review_request > first_comment_on_first_review.best:
            # we cannot request a review after we received a review
            first_review_request = Fallback(first_review_request_backup.best, None)
        else:
            first_review_request = Fallback(first_review_request, first_review_request_backup)
        # ensure that the first review request is not earlier than the last commit before
        # the first review
        if last_commit_before_first_review.value is not None and \
                last_commit_before_first_review > first_review_request:
            first_review_request = Fallback(
                last_commit_before_first_review.value, first_review_request)
        review_submitted_ats = pr.reviews[PullRequestReview.submitted_at.key]
        if closed_at:
            not_review_comments = \
                pr.reviews[PullRequestReview.state.key].values != ReviewResolution.COMMENTED.value
            # it is possible to approve/reject after closing the PR
            # you start the review, then somebody closes the PR, then you submit the review
            try:
                last_review_at = pd.Timestamp(review_submitted_ats.values[
                    (review_submitted_ats.values <= closed_at.best.to_numpy())
                    | not_review_comments].max(), tz=timezone.utc)
            except ValueError:
                last_review_at = pd.NaT
            if last_review_at == last_review_at:
                # we don't want dtmin() here - what if there was no review at all?
                last_review_at = min(last_review_at, closed_at.best)
            last_review = Fallback(
                last_review_at,
                dtmin(external_comments_times.take(np.where(
                    external_comments_times <= closed_at.best)[0]).max()))
        else:
            last_review = Fallback(review_submitted_ats.max(),
                                   dtmin(external_comments_times.max()))
        if merged_at:
            reviews_before_merge = \
                pr.reviews[PullRequestReview.submitted_at.key].values <= merged_at.best.to_numpy()
            if reviews_before_merge.all():
                reviews_before_merge = pr.reviews
            else:
                reviews_before_merge = pr.reviews.take(np.where(reviews_before_merge)[0])
        else:
            reviews_before_merge = pr.reviews
        # the most recent review for each reviewer
        if reviews_before_merge.empty:
            # express lane
            grouped_reviews = self.dummy_reviews
        elif reviews_before_merge[PullRequestReview.user_id.key].nunique() == 1:
            # fast lane
            grouped_reviews = reviews_before_merge._ixs(
                reviews_before_merge[PullRequestReview.submitted_at.key].values.argmax())
        else:
            # the most recent review for each reviewer
            latest_review_ixs = np.where(
                reviews_before_merge[[PullRequestReview.user_id.key,
                                      PullRequestReview.submitted_at.key]]
                .take(np.where(reviews_before_merge[PullRequestReview.state.key] !=
                               ReviewResolution.COMMENTED.value)[0])
                .sort_values([PullRequestReview.submitted_at.key],
                             ascending=False, ignore_index=True)
                .groupby(PullRequestReview.user_id.key, sort=False, as_index=False)
                ._cumcount_array())[0]
            grouped_reviews = {
                k: reviews_before_merge[k].take(latest_review_ixs)
                for k in (PullRequestReview.state.key, PullRequestReview.submitted_at.key)}
        grouped_reviews_states = grouped_reviews[PullRequestReview.state.key]
        if isinstance(grouped_reviews_states, str):
            changes_requested = grouped_reviews_states == ReviewResolution.CHANGES_REQUESTED.value
        else:
            changes_requested = (
                grouped_reviews_states.values == ReviewResolution.CHANGES_REQUESTED.value
            ).any()
        if changes_requested:
            # merged with negative reviews
            approved_at_value = None
        else:
            if isinstance(grouped_reviews_states, str):
                if grouped_reviews_states == ReviewResolution.APPROVED.value:
                    approved_at_value = grouped_reviews[PullRequestReview.submitted_at.key]
                else:
                    approved_at_value = pd.NaT
            else:
                approved_at_value = grouped_reviews[PullRequestReview.submitted_at.key].take(
                    np.where(grouped_reviews_states == ReviewResolution.APPROVED.value)[0]).max()
            if approved_at_value == approved_at_value and closed_at:
                # similar to last_review
                approved_at_value = min(approved_at_value, closed_at.best)
        approved_at = Fallback(approved_at_value, None)
        last_passed_checks = Fallback(None, None)  # FIXME(vmarkovtsev): no CI info
        released_at = Fallback(pr.release[Release.published_at.key], None)
        additions = pr.pr[PullRequest.additions.key]
        deletions = pr.pr[PullRequest.deletions.key]
        if additions is None or deletions is None:
            self.log.error("NULL in PR additions or deletions: %s (%s#%d): +%s -%s",
                           pr.pr[PullRequest.node_id.key],
                           pr.pr[PullRequest.repository_full_name.key],
                           pr.pr[PullRequest.number.key],
                           additions, deletions)
            raise ImpossiblePullRequest()
        size = additions + deletions
        force_push_dropped = pr.release[matched_by_column] == ReleaseMatch.force_push_drop
        facts = PullRequestFacts(
            created=created_at,
            first_commit=first_commit,
            last_commit_before_first_review=last_commit_before_first_review,
            last_commit=last_commit,
            merged=merged_at,
            first_comment_on_first_review=first_comment_on_first_review,
            first_review_request=first_review_request,
            last_review=last_review,
            approved=approved_at,
            first_passed_checks=Fallback(None, None),  # FIXME(vmarkovtsev): no CI info
            last_passed_checks=last_passed_checks,
            released=released_at,
            closed=closed_at,
            size=size,
            force_push_dropped=force_push_dropped,
        )
        self._validate(facts, pr.pr[PullRequest.htmlurl.key])
        return facts

    def _validate(self, facts: PullRequestFacts, url: str) -> None:
        """Run sanity checks to ensure consistency."""
        if not facts.closed:
            return
        if facts.last_commit and facts.last_commit.best > facts.closed.best:
            self.log.error("%s is impossible: closed %s but last commit %s: delta %s",
                           url, facts.closed.best, facts.last_commit.best,
                           facts.closed.best - facts.last_commit.best)
            raise ImpossiblePullRequest()
        if facts.created.best > facts.closed.best:
            self.log.error("%s is impossible: closed %s but created %s: delta %s",
                           url, facts.closed.best, facts.created.best,
                           facts.closed.best - facts.created.best)
            raise ImpossiblePullRequest()
        if facts.merged and facts.released and facts.merged.best > facts.released.best:
            self.log.error("%s is impossible: merged %s but released %s: delta %s",
                           url, facts.merged.best, facts.released.best,
                           facts.released.best - facts.merged.best)
            raise ImpossiblePullRequest()
