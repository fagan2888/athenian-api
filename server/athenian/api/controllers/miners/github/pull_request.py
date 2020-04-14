import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
import io
import struct
from typing import Collection, Dict, Generator, Generic, List, Optional, TypeVar, \
    Union

import aiomcache
import databases
import pandas as pd
from sqlalchemy import select, sql
from sqlalchemy.orm.attributes import InstrumentedAttribute

from athenian.api.async_read_sql_query import read_sql_query
from athenian.api.cache import cached
from athenian.api.controllers.miners.github.hardcoded import BOTS
from athenian.api.controllers.miners.github.release import map_prs_to_releases, \
    map_releases_to_prs
from athenian.api.controllers.settings import ReleaseMatchSetting
from athenian.api.models.metadata.github import Base, PullRequest, PullRequestComment, \
    PullRequestCommit, PullRequestReview, PullRequestReviewComment, PullRequestReviewRequest, \
    Release


@dataclass(frozen=True)
class MinedPullRequest:
    """All the relevant information we are able to load from the metadata DB about a PR.

    All the DataFrame-s have a two-layered index:
    1. pull request id
    2. own id
    The artificial first index layer makes it is faster to select data belonging to a certain PR.
    """

    pr: pd.Series
    reviews: pd.DataFrame
    review_comments: pd.DataFrame
    review_requests: pd.DataFrame
    comments: pd.DataFrame
    commits: pd.DataFrame
    release: pd.Series


class PullRequestMiner:
    """Load all the information related to Pull Requests from the metadata DB. Iterate over it \
    with individual PR tuples."""

    CACHE_TTL = 5 * 60

    def __init__(self, prs: pd.DataFrame, reviews: pd.DataFrame, review_comments: pd.DataFrame,
                 review_requests: pd.DataFrame, comments: pd.DataFrame, commits: pd.DataFrame,
                 releases: pd.DataFrame):
        """Initialize a new instance of `PullRequestMiner`."""
        self._prs = prs
        self._reviews = reviews
        self._review_comments = review_comments
        self._review_requests = review_requests
        self._comments = comments
        self._commits = commits
        self._releases = releases

    def _serialize_for_cache(dfs: List[pd.DataFrame]) -> memoryview:
        assert len(dfs) < 256
        buf = io.BytesIO()
        buf.close = lambda: None  # disable closing the buffer in to_pickle()
        offsets = []
        for df in dfs:
            # pickle works 2-3x faster for both serialization and deserialization on our data
            df.to_pickle(buf)
            # tracking of the offsets is not really needed for pickles, but is required by feather
            # we don't use feather *yet* due to https://github.com/pandas-dev/pandas/issues/32587
            # FIXME(vmarkovtsev): ^^^
            offsets.append(buf.tell())
        buf.write(struct.pack("!" + "I" * len(offsets), *offsets))
        buf.write(struct.pack("!B", len(offsets)))
        return buf.getbuffer()

    def _deserialize_from_cache(data: bytes) -> List[pd.DataFrame]:
        data = memoryview(data)
        size = struct.unpack("!B", data[-1:])[0]
        offsets = (0,) + struct.unpack("!" + "I" * size, data[-size * 4 - 1:-1])
        dfs = []
        for beg, end in zip(offsets, offsets[1:]):
            df = pd.read_pickle(io.BytesIO(data[beg:end]))
            # The following code recovers the index if it was discarded.
            """
            if "pull_request_node_id" in df.columns:
                df.set_index(["pull_request_node_id", "node_id"], inplace=True)
            else:
                df.set_index("node_id", inplace=True)
            """
            dfs.append(df)
        return dfs

    @classmethod
    @cached(
        exptime=lambda cls, **_: cls.CACHE_TTL,
        serialize=_serialize_for_cache,
        deserialize=_deserialize_from_cache,
        key=lambda time_from, time_to, repositories, developers, **_: (
            time_from.toordinal(),
            time_to.toordinal(),
            ",".join(sorted(repositories)),
            ",".join(sorted(developers)),
        ),
    )
    async def _mine(cls, time_from: date, time_to: date, repositories: Collection[str],
                    release_settings: Dict[str, ReleaseMatchSetting],
                    developers: Collection[str], db: databases.Database,
                    cache: Optional[aiomcache.Client],
                    ) -> List[pd.DataFrame]:
        assert isinstance(time_from, date) and not isinstance(time_from, datetime)
        assert isinstance(time_to, date) and not isinstance(time_to, datetime)
        time_from, time_to = (pd.Timestamp(t, tzinfo=timezone.utc) for t in (time_from, time_to))
        filters = [
            sql.or_(PullRequest.updated_at.between(time_from, time_to),
                    sql.and_(sql.or_(PullRequest.closed_at.is_(None),
                                     PullRequest.closed_at > time_from),
                             PullRequest.created_at < time_to)),
            PullRequest.repository_full_name.in_(repositories),
        ]
        if len(developers) > 0:
            filters.append(PullRequest.user_login.in_(developers))
        async with db.connection() as conn:
            prs = await read_sql_query(select([PullRequest]).where(sql.and_(*filters)),
                                       conn, PullRequest, index=PullRequest.node_id.key)
            released_prs = await map_releases_to_prs(
                repositories, time_from, time_to, release_settings, conn, cache)
            prs = pd.concat([prs, released_prs], sort=False)
        cls.truncate_timestamps(prs, time_to)
        node_ids = prs.index if len(prs) > 0 else set()

        async def fetch_reviews():
            return await cls._read_filtered_models(
                db, PullRequestReview, node_ids, time_to,
                columns=[PullRequestReview.submitted_at, PullRequestReview.user_id,
                         PullRequestReview.state, PullRequestReview.user_login])

        async def fetch_review_comments():
            return await cls._read_filtered_models(
                db, PullRequestReviewComment, node_ids, time_to,
                columns=[PullRequestReviewComment.created_at, PullRequestReviewComment.user_id])

        async def fetch_review_requests():
            return await cls._read_filtered_models(
                db, PullRequestReviewRequest, node_ids, time_to,
                columns=[PullRequestReviewRequest.created_at])

        async def fetch_comments():
            return await cls._read_filtered_models(
                db, PullRequestComment, node_ids, time_to,
                columns=[PullRequestComment.created_at, PullRequestComment.user_id,
                         PullRequestComment.user_login])

        async def fetch_commits():
            return await cls._read_filtered_models(
                db, PullRequestCommit, node_ids, time_to,
                columns=[PullRequestCommit.authored_date, PullRequestCommit.committed_date,
                         PullRequestCommit.author_login, PullRequestCommit.committer_login])

        async def map_releases():
            merged_prs = prs[prs[PullRequest.merged_at.key].between(time_from, time_to)]
            return await map_prs_to_releases(merged_prs, time_to, release_settings, db, cache)

        dfs = await asyncio.gather(
            fetch_reviews(), fetch_review_comments(), fetch_review_requests(), fetch_comments(),
            fetch_commits(), map_releases())
        for df in dfs:
            cls.truncate_timestamps(df, time_to)
        reviews, review_comments, review_requests, comments, commits, releases = dfs
        return [prs, reviews, review_comments, review_requests, comments, commits, releases]

    _serialize_for_cache = staticmethod(_serialize_for_cache)
    _deserialize_from_cache = staticmethod(_deserialize_from_cache)

    @classmethod
    async def mine(cls, time_from: date, time_to: date, repositories: Collection[str],
                   release_settings: Dict[str, ReleaseMatchSetting],
                   developers: Collection[str], db: databases.Database,
                   cache: Optional[aiomcache.Client]) -> "PullRequestMiner":
        """
        Create a new `PullRequestMiner` from the metadata DB according to the specified filters.

        :param time_from: Fetch PRs created starting from this date, inclusive.
        :param time_to: Fetch PRs created ending with this date, inclusive.
        :param repositories: PRs must belong to these repositories (prefix excluded).
        :param developers: PRs must be authored by these user IDs. An empty list means everybody.
        :param db: Metadata db instance.
        :param cache: memcached client to cache the collected data.
        """
        dfs = await cls._mine(time_from, time_to, repositories, release_settings, developers,
                              db, cache)
        return cls(*dfs)

    @staticmethod
    async def _read_filtered_models(conn: Union[databases.core.Connection, databases.Database],
                                    model_cls: Base,
                                    node_ids: Collection[str],
                                    time_to: datetime,
                                    columns: Optional[List[InstrumentedAttribute]] = None,
                                    ) -> pd.DataFrame:
        if columns is not None:
            columns = [model_cls.pull_request_node_id, model_cls.node_id] + columns
        df = await read_sql_query(select(columns or [model_cls]).where(
            sql.and_(model_cls.pull_request_node_id.in_(node_ids),
                     model_cls.created_at < time_to)),
            con=conn,
            columns=columns or model_cls,
            index=[model_cls.pull_request_node_id.key, model_cls.node_id.key])
        return df

    @staticmethod
    def truncate_timestamps(df: pd.DataFrame, upto: datetime):
        """Set all the timestamps after `upto` to NaT to avoid "future leakages"."""
        for col in df.select_dtypes(include=[object]):
            try:
                df[df[col] > upto, col] = pd.NaT
            except TypeError:
                continue
        for col in df.select_dtypes(include=["datetime"]):
            df[df[col] > upto, col] = pd.NaT

    def __iter__(self) -> Generator[MinedPullRequest, None, None]:
        """Iterate over the individual pull requests."""
        for pr_node_id, pr in self._prs.iterrows():
            items = {}
            for k in MinedPullRequest.__dataclass_fields__:
                if k == "pr":
                    continue
                df = getattr(self, "_" + (k if k.endswith("s") else k + "s"))
                try:
                    items[k] = df.loc[pr_node_id]
                except KeyError:
                    if k.endswith("s"):
                        items[k] = df.iloc[:0].copy()
                    else:
                        items[k] = pd.Series(
                            [None] * len(df.columns), index=df.columns, name="empty " + k)
            yield MinedPullRequest(pr, **items)


T = TypeVar("T")


class Fallback(Generic[T]):
    """
    A value with a "plan B".

    The idea is to return the backup in `Fallback.best` if the primary value is absent (None).
    We can check whether the primary value exists by `Fallback.value is None`.
    """

    def __init__(self, value: Optional[T], fallback: Union[None, T, "Fallback[T]"]):
        """Initialize a new instance of `Fallback`."""
        if value != value:  # NaN check
            value = None
        self.__value = value
        self.__fallback = fallback

    @property
    def best(self) -> Optional[T]:
        """The "best effort" value, either the primary or the backup one."""  # noqa: D401
        if self.__value is not None:
            return self.__value
        if isinstance(self.__fallback, Fallback):
            return self.__fallback.best
        return self.__fallback

    def __str__(self) -> str:
        """str()."""
        return "Fallback(%s, %s)" % (self.value, self.best)

    def __repr__(self) -> str:
        """repr()."""
        return "Fallback(%r, %r)" % (self.value, self.best)

    def __bool__(self) -> bool:
        """Return the value indicating whether there is any value, either primary or backup."""
        return self.best is not None

    def __lt__(self, other: "Fallback[T]") -> bool:
        """Implement <."""
        if not self or not other:
            raise ArithmeticError
        return self.best < other.best

    def __eq__(self, other: "Fallback[T]") -> bool:
        """Implement ==."""
        if not self or not other:
            raise ArithmeticError
        return self.best == other.best

    def __le__(self, other: "Fallback[T]") -> bool:
        """Implement <=."""
        if not self or not other:
            raise ArithmeticError
        return self.best <= other.best

    @property
    def value(self) -> Optional[T]:
        """The primary value."""  # noqa: D401
        return self.__value

    @classmethod
    def max(cls, *args: "Fallback[T]") -> "Fallback[T]":
        """Calculate the maximum of several Fallback.best-s."""
        return cls.agg(max, *args)

    @classmethod
    def min(cls, *args: "Fallback[T]") -> "Fallback[T]":
        """Calculate the minimum of several Fallback.best-s."""
        return cls.agg(min, *args)

    @classmethod
    def agg(cls, func: callable, *args: "Fallback[T]") -> "Fallback[T]":
        """Calculate an aggregation of several Fallback.best-s."""
        try:
            return cls(func(arg.best for arg in args if arg.best is not None), None)
        except ValueError:
            return cls(None, None)


DT = Union[pd.Timestamp, datetime, None]


@dataclass(frozen=True)
class PullRequestTimes:
    """Various PR update timestamps."""

    @property
    def work_began(self) -> Fallback[DT]:  # PR_B   noqa: D102
        return Fallback.min(self.created, self.first_commit)

    created: Fallback[DT]                                # PR_C
    first_commit: Fallback[DT]                           # PR_CC
    last_commit_before_first_review: Fallback[DT]        # PR_CFR
    last_commit: Fallback[DT]                            # PR_LC
    merged: Fallback[DT]                                 # PR_M
    closed: Fallback[DT]                                 # PR_CL
    first_comment_on_first_review: Fallback[DT]          # PR_W
    first_review_request: Fallback[DT]                   # PR_S
    approved: Fallback[DT]                               # PR_A
    last_review: Fallback[DT]                            # PR_LR
    first_passed_checks: Fallback[DT]                    # PR_VS
    last_passed_checks: Fallback[DT]                     # PR_VF
    released: Fallback[DT]                               # PR_R

    def max_timestamp(self) -> DT:
        """Find the maximum timestamp contained in the struct."""
        return Fallback.max(*self.__dict__.values()).best

    def __str__(self) -> str:
        """Format for human-readability."""
        return "{\n\t%s\n}" % ",\n\t".join("%s: %s" % (k, v.best) for k, v in vars(self).items())


class ReviewResolution(Enum):
    """Possible review "state"-s in the metadata DB."""

    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    COMMENTED = "COMMENTED"


class PullRequestTimesMiner(PullRequestMiner):
    """Extract the pull request update timestamps from the metadata DB."""

    def _compile(self, pr: MinedPullRequest) -> PullRequestTimes:
        created_at = Fallback(pr.pr[PullRequest.created_at.key], None)
        merged_at = Fallback(pr.pr[PullRequest.merged_at.key], None)
        closed_at = Fallback(pr.pr[PullRequest.closed_at.key], None)
        first_commit = Fallback(pr.commits[PullRequestCommit.committed_date.key].min(), None)
        last_commit = Fallback(pr.commits[PullRequestCommit.committed_date.key].max(), None)
        authored_comments = pr.comments[PullRequestReviewComment.user_id.key]
        external_comments_times = pr.comments.loc[
            (authored_comments != pr.pr[PullRequest.user_id.key]) & ~authored_comments.isin(BOTS),
            PullRequestComment.created_at.key]
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
                committed_dates[committed_dates <= first_comment_on_first_review.best].max(),
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
        if closed_at:
            submitted_ats = pr.reviews[PullRequestReview.submitted_at.key]
            last_review = Fallback(
                submitted_ats[submitted_ats <= closed_at.best].max(),
                dtmin(external_comments_times[external_comments_times < closed_at.best].max()))
        else:
            last_review = Fallback(
                pr.reviews[PullRequestReview.submitted_at.key].max(),
                dtmin(external_comments_times.max()))
        if merged_at:
            reviews_before_merge = pr.reviews[
                pr.reviews[PullRequestReview.submitted_at.key] <= merged_at.best]
        else:
            reviews_before_merge = pr.reviews
        grouped_reviews = reviews_before_merge \
            .sort_values([PullRequestReview.submitted_at.key], ascending=True) \
            .groupby(PullRequestReview.user_id.key, sort=False) \
            .first()  # the most recent review for each reviewer
        if (grouped_reviews[PullRequestReview.state.key]
                == ReviewResolution.CHANGES_REQUESTED.value).any():
            # merged with negative reviews
            approved_at_value = None
        else:
            approved_at_value = grouped_reviews.loc[
                grouped_reviews[PullRequestReview.state.key] == ReviewResolution.APPROVED.value,
                PullRequestReview.submitted_at.key].max()
        approved_at = Fallback(approved_at_value, None)
        last_passed_checks = Fallback(None, None)  # FIXME(vmarkovtsev): no CI info
        released_at = Fallback(pr.release[Release.published_at.key], None)
        return PullRequestTimes(
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
        )

    def __iter__(self) -> Generator[PullRequestTimes, None, None]:
        """Iterate over the individual pull requests."""
        for pr in super().__iter__():
            yield self._compile(pr)


def dtmin(*args: Union[DT, float]) -> DT:
    """Find the minimum of several dates handling NaNs gracefully."""
    if all((arg != arg) for arg in args):
        return None
    return min(arg for arg in args if arg == arg)
