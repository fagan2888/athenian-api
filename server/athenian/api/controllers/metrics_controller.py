from collections import defaultdict
from itertools import chain
from typing import Dict, List, Sequence, Set, Tuple

from aiohttp import web
import databases.core

from athenian.api.async_utils import gather
from athenian.api.balancing import weight
from athenian.api.controllers.account import get_metadata_account_ids
from athenian.api.controllers.datetime_utils import split_to_time_intervals
from athenian.api.controllers.features.code import CodeStats
from athenian.api.controllers.features.entries import METRIC_ENTRIES
from athenian.api.controllers.jira import get_jira_installation, get_jira_installation_or_none
from athenian.api.controllers.miners.access_classes import access_classes, AccessChecker
from athenian.api.controllers.miners.filters import JIRAFilter, LabelFilter
from athenian.api.controllers.miners.github.commit import FilterCommitsProperty
from athenian.api.controllers.miners.github.developer import DeveloperTopic
from athenian.api.controllers.miners.types import PRParticipants, PRParticipationKind, \
    ReleaseParticipationKind
from athenian.api.controllers.reposet import resolve_repos, resolve_reposet
from athenian.api.controllers.settings import Settings
from athenian.api.models.metadata import PREFIXES
from athenian.api.models.web import CalculatedDeveloperMetrics, CalculatedDeveloperMetricsItem, \
    CalculatedLinearMetricValues, CalculatedPullRequestMetrics, CalculatedPullRequestMetricsItem, \
    CalculatedReleaseMetric, CodeBypassingPRsMeasurement, CodeFilter, DeveloperMetricsRequest, \
    ForbiddenError, ForSet, ForSetDevelopers, ReleaseMetricsRequest
from athenian.api.models.web.invalid_request_error import InvalidRequestError
from athenian.api.models.web.pull_request_metrics_request import PullRequestMetricsRequest
from athenian.api.request import AthenianWebRequest
from athenian.api.response import model_response, ResponseError
from athenian.api.tracing import sentry_span

#               service                          developers                                originals  # noqa
FilterPRs = Tuple[str, Tuple[List[Set[str]], List[PRParticipants], LabelFilter, JIRAFilter, ForSet]]  # noqa
#                             repositories

#                service                     developers
FilterDevs = Tuple[str, Tuple[List[Set[str]], List[str], ForSetDevelopers]]
#                              repositories                  originals


@weight(10)
async def calc_metrics_pr_linear(request: AthenianWebRequest, body: dict) -> web.Response:
    """Calculate linear metrics over PRs.

    :param request: HTTP request.
    :param body: Desired metric definitions.
    :type body: dict | bytes
    """
    try:
        filt = PullRequestMetricsRequest.from_dict(body)
    except ValueError as e:
        # for example, passing a date with day=32
        return ResponseError(InvalidRequestError("?", detail=str(e))).response
    meta_ids = await get_metadata_account_ids(filt.account, request.sdb, request.cache)
    filters, repos = await compile_repos_and_devs_prs(filt.for_, request, filt.account, meta_ids)
    time_intervals, tzoffset = split_to_time_intervals(
        filt.date_from, filt.date_to, filt.granularities, filt.timezone)

    """
    @se7entyse7en:
    It seems weird to me that the generated class constructor accepts None as param and it
    doesn't on setters. Probably it would have much more sense to generate a class that doesn't
    accept the params at all or that it does not default to None. :man_shrugging:

    @vmarkovtsev:
    This is exactly what I did the other day. That zalando/connexion thingie which glues OpenAPI
    and asyncio together constructs all the models by calling their __init__ without any args and
    then setting individual attributes. So we crash somewhere in from_dict() or to_dict() if we
    make something required.
    """
    met = CalculatedPullRequestMetrics()
    met.date_from = filt.date_from
    met.date_to = filt.date_to
    met.timezone = filt.timezone
    met.granularities = filt.granularities
    met.quantiles = filt.quantiles
    met.metrics = filt.metrics
    met.exclude_inactive = filt.exclude_inactive
    met.calculated = []

    release_settings = \
        await Settings.from_request(request, filt.account).list_release_matches(repos)

    @sentry_span
    async def calculate_for_set_metrics(service, repos, withgroups, labels, jira, for_set):
        metric_values = await METRIC_ENTRIES[service]["prs_linear"](
            filt.metrics, time_intervals, filt.quantiles or (0, 1),
            for_set.lines or [], repos, withgroups, labels, jira,
            filt.exclude_inactive, release_settings, filt.fresh,
            filt.account, meta_ids, request.mdb, request.pdb, request.rdb, request.cache)
        mrange = range(len(met.metrics))
        for lines_group_index, lines_group in enumerate(metric_values):
            for repos_group_index, with_groups in enumerate(lines_group):
                for with_group_index, repos_group in enumerate(with_groups):
                    group_for_set = for_set \
                        .select_lines(lines_group_index) \
                        .select_repogroup(repos_group_index) \
                        .select_withgroup(with_group_index)
                    for granularity, ts, mvs in zip(filt.granularities,
                                                    time_intervals,
                                                    repos_group):
                        cm = CalculatedPullRequestMetricsItem(
                            for_=group_for_set,
                            granularity=granularity,
                            values=[CalculatedLinearMetricValues(
                                date=(d - tzoffset).date(),
                                values=[mvs[i][m].value for m in mrange],
                                confidence_mins=[mvs[i][m].confidence_min for m in mrange],
                                confidence_maxs=[mvs[i][m].confidence_max for m in mrange],
                                confidence_scores=[mvs[i][m].confidence_score() for m in mrange],
                            ) for i, d in enumerate(ts[:-1])])
                        for v in cm.values:
                            if sum(1 for c in v.confidence_scores if c is not None) == 0:
                                v.confidence_mins = None
                                v.confidence_maxs = None
                                v.confidence_scores = None
                        met.calculated.append(cm)
    tasks = []
    for service, (repos, withgroups, labels, jira, for_set) in filters:
        tasks.append(calculate_for_set_metrics(service, repos, withgroups, labels, jira, for_set))
    await gather(*tasks)
    return model_response(met)


async def compile_repos_and_devs_prs(for_sets: List[ForSet],
                                     request: AthenianWebRequest,
                                     account: int,
                                     meta_ids: Tuple[int, ...],
                                     ) -> Tuple[List[FilterPRs], Set[str]]:
    """
    Build the list of filters for a given list of ForSet-s.

    Repository sets are dereferenced. Access permissions are checked.

    :param for_sets: Paired lists of repositories, developers, and PR labels.
    :param request: Our incoming request to take the metadata DB, the user ID, the cache.
    :param account: Account ID on behalf of which we are loading reposets.
    :return: 1. Resulting list of filters; \
             2. The set of all repositories after dereferencing, with service prefixes.
    """
    filters = []
    checkers = {}
    all_repos = set()
    async with request.sdb.connection() as sdb_conn:
        for i, for_set in enumerate(for_sets):
            repos, service = await _extract_repos(
                request, account, meta_ids, for_set.repositories, i, all_repos, checkers, sdb_conn)
            if for_set.repogroups is not None:
                repogroups = [set(chain.from_iterable(repos[i] for i in group))
                              for group in for_set.repogroups]
            else:
                repogroups = [set(chain.from_iterable(repos))]
            prefix = PREFIXES[service]
            withgroups = []
            for with_ in (for_set.withgroups or []) + ([for_set.with_] if for_set.with_ else []):
                withgroup = {}
                for k, v in with_.items():
                    if not v:
                        continue
                    withgroup[PRParticipationKind[k.upper()]] = dk = set()
                    for dev in v:
                        if not dev.startswith(prefix):
                            raise ResponseError(InvalidRequestError(
                                detail='providers in "with" and "repositories" do not match',
                                pointer=".for[%d].with" % i,
                            ))
                        dk.add(dev[len(prefix):])
                if withgroup:
                    withgroups.append(withgroup)
            labels = LabelFilter.from_iterables(for_set.labels_include, for_set.labels_exclude)
            try:
                jira = JIRAFilter.from_web(
                    for_set.jira,
                    await get_jira_installation(account, request.sdb, request.mdb, request.cache))
            except ResponseError:
                jira = JIRAFilter.empty()
            filters.append((service, (repogroups, withgroups, labels, jira, for_set)))
    return filters, all_repos


async def _compile_repos_and_devs_devs(for_sets: List[ForSetDevelopers],
                                       request: AthenianWebRequest,
                                       account: int,
                                       meta_ids: Tuple[int, ...],
                                       ) -> (List[FilterDevs], List[str]):
    """
    Build the list of filters for a given list of ForSetDevelopers'.

    Repository sets are de-referenced. Access permissions are checked.

    :param for_sets: Paired lists of repositories and developers.
    :param request: Our incoming request to take the metadata DB, the user ID, the cache.
    :param account: Account ID on behalf of which we are loading reposets.
    :param meta_ids: Metadata (GitHub) account IDs.
    :return: Resulting list of filters and the list of all repositories after dereferencing, \
             with service prefixes.
    """
    filters = []
    checkers = {}
    all_repos = set()
    async with request.sdb.connection() as sdb_conn:
        for i, for_set in enumerate(for_sets):
            repos, service = await _extract_repos(
                request, account, meta_ids, for_set.repositories, i, all_repos, checkers, sdb_conn)
            if for_set.repogroups is not None:
                repogroups = [set(chain.from_iterable(repos[i] for i in group))
                              for group in for_set.repogroups]
            else:
                repogroups = [set(chain.from_iterable(repos))]
            prefix = PREFIXES[service]
            devs = []
            for dev in for_set.developers:
                if not dev.startswith(prefix):
                    raise ResponseError(InvalidRequestError(
                        detail='providers in "developers" and "repositories" do not match',
                        pointer=".for[%d].developers" % i,
                    ))
                devs.append(dev[len(prefix):])
            labels = LabelFilter.from_iterables(for_set.labels_include, for_set.labels_exclude)
            try:
                jira = JIRAFilter.from_web(
                    for_set.jira,
                    await get_jira_installation(account, request.sdb, request.mdb, request.cache))
            except ResponseError:
                jira = JIRAFilter.empty()
            filters.append((service, (repogroups, devs, labels, jira, for_set)))
    return filters, all_repos


async def _extract_repos(request: AthenianWebRequest,
                         account: int,
                         meta_ids: Tuple[int, ...],
                         for_set: List[str],
                         for_set_index: int,
                         all_repos: Set[str],
                         checkers: Dict[str, AccessChecker],
                         sdb: databases.core.Connection) -> Tuple[Sequence[Set[str]], str]:
    user = request.uid
    service = None
    resolved = await gather(*[
        resolve_reposet(r, ".for[%d].repositories[%d]" % (
            for_set_index, j), user, account, sdb, request.cache)
        for j, r in enumerate(for_set)], op="resolve_reposet-s")
    for repos in resolved:
        for i, repo in enumerate(repos):
            for key, prefix in PREFIXES.items():
                if repo.startswith(prefix):
                    if service is None:
                        service = key
                    elif service != key:
                        raise ResponseError(InvalidRequestError(
                            detail='mixed providers are not allowed in the same "for" element',
                            pointer=".for[%d].repositories" % for_set_index,
                        ))
                    repos[i] = repo[len(prefix):]
                    all_repos.add(repo)
    if service is None:
        raise ResponseError(InvalidRequestError(
            detail='the provider of a "for" element is unsupported or the set is empty',
            pointer=".for[%d].repositories" % for_set_index,
        ))
    if (checker := checkers.get(service)) is None:
        checkers[service] = (checker := await access_classes[service](
            account, meta_ids, sdb, request.mdb, request.cache,
        ).load())
    if denied := await checker.check(set(chain.from_iterable(resolved))):
        raise ResponseError(ForbiddenError(
            detail="some repositories in .for[%d].repositories are access denied on %s: %s" % (
                for_set_index, service, denied),
        ))
    return resolved, service


@weight(1.5)
async def calc_code_bypassing_prs(request: AthenianWebRequest, body: dict) -> web.Response:
    """Measure the amount of code that was pushed outside of pull requests."""
    try:
        filt = CodeFilter.from_dict(body)
    except ValueError as e:
        # for example, passing a date with day=32
        return ResponseError(InvalidRequestError("?", detail=str(e))).response

    async def login_loader() -> str:
        return (await request.user()).login

    repos, meta_ids = await resolve_repos(
        filt.in_, filt.account, request.uid, login_loader,
        request.sdb, request.mdb, request.cache, request.app["slack"])
    time_intervals, tzoffset = split_to_time_intervals(
        filt.date_from, filt.date_to, filt.granularity, filt.timezone)
    with_author = [s.split("/", 1)[1] for s in (filt.with_author or [])]
    with_committer = [s.split("/", 1)[1] for s in (filt.with_committer or [])]
    stats = await METRIC_ENTRIES["github"]["code"](
        FilterCommitsProperty.BYPASSING_PRS, time_intervals, repos, with_author,
        with_committer, meta_ids, request.mdb, request.cache)  # type: List[CodeStats]
    model = [
        CodeBypassingPRsMeasurement(
            date=(d - tzoffset).date(),
            bypassed_commits=s.queried_number_of_commits,
            bypassed_lines=s.queried_number_of_lines,
            total_commits=s.total_number_of_commits,
            total_lines=s.total_number_of_lines,
        )
        for d, s in zip(time_intervals[:-1], stats)]
    return model_response(model)


@weight(1.5)
async def calc_metrics_developer(request: AthenianWebRequest, body: dict) -> web.Response:
    """Calculate metrics over developer activities."""
    try:
        filt = DeveloperMetricsRequest.from_dict(body)
    except ValueError as e:
        # for example, passing a date with day=32
        return ResponseError(InvalidRequestError("?", detail=str(e))).response
    meta_ids = await get_metadata_account_ids(filt.account, request.sdb, request.cache)
    filters, all_repos = await _compile_repos_and_devs_devs(
        filt.for_, request, filt.account, meta_ids)
    if filt.date_to < filt.date_from:
        raise ResponseError(InvalidRequestError(
            detail="date_from may not be greater than date_to",
            pointer=".date_from",
        ))
    release_settings = \
        await Settings.from_request(request, filt.account).list_release_matches(all_repos)

    met = CalculatedDeveloperMetrics()
    met.date_from = filt.date_from
    met.date_to = filt.date_to
    met.timezone = filt.timezone
    met.metrics = filt.metrics
    met.calculated = []
    topics = {DeveloperTopic(t) for t in filt.metrics}
    time_from, time_to = filt.resolve_time_from_and_to()
    tasks = []
    for_sets = []
    for service, (repos, devs, labels, jira, for_set) in filters:
        tasks.append(METRIC_ENTRIES[service]["developers"](
            devs, repos, time_from, time_to, topics, labels, jira, release_settings,
            filt.account, meta_ids, request.mdb, request.pdb, request.rdb, request.cache))
        for_sets.append(for_set)
    all_stats = await gather(*tasks)
    for stats, for_set in zip(all_stats, for_sets):
        for i, group in enumerate(stats):
            if for_set.aggregate_devgroups:
                values = [[sum(getattr(group[i], DeveloperTopic(t).name) for i in devgroup)
                           for t in filt.metrics]
                          for devgroup in for_set.aggregate_devgroups]
            else:
                values = [[getattr(s, DeveloperTopic(t).name) for t in filt.metrics]
                          for s in group]
            met.calculated.append(CalculatedDeveloperMetricsItem(
                for_=for_set.select_repogroup(i),
                values=values,
            ))
    return model_response(met)


async def _compile_repos_releases(request: AthenianWebRequest,
                                  for_sets: List[List[str]],
                                  account: int,
                                  meta_ids: Tuple[int, ...],
                                  ) -> Tuple[List[Tuple[str, Tuple[Set[str], List[str]]]],
                                             Set[str]]:
    filters = []
    checkers = {}
    all_repos = set()
    async with request.sdb.connection() as sdb_conn:
        for i, for_set in enumerate(for_sets):
            repos, service = await _extract_repos(
                request, account, meta_ids, for_set, i, all_repos, checkers, sdb_conn)
            filters.append((service, (set(chain.from_iterable(repos)), for_set)))
    return filters, all_repos


@weight(4)
async def calc_metrics_releases_linear(request: AthenianWebRequest, body: dict) -> web.Response:
    """Calculate linear metrics over releases."""
    try:
        filt = ReleaseMetricsRequest.from_dict(body)
    except ValueError as e:
        # for example, passing a date with day=32
        return ResponseError(InvalidRequestError("?", detail=str(e))).response
    meta_ids = await get_metadata_account_ids(filt.account, request.sdb, request.cache)
    filters, repos = await _compile_repos_releases(request, filt.for_, filt.account, meta_ids)
    grouped_for_sets = defaultdict(list)
    grouped_repos = defaultdict(list)
    for service, (for_set_repos, for_set) in filters:
        grouped_for_sets[service].append(for_set)
        grouped_repos[service].append(for_set_repos)
    del filters
    time_intervals, tzoffset = split_to_time_intervals(
        filt.date_from, filt.date_to, filt.granularities, filt.timezone)
    tasks = [
        Settings.from_request(request, filt.account).list_release_matches(repos),
        get_jira_installation_or_none(filt.account, request.sdb, request.mdb, request.cache),
    ]
    release_settings, jira_ids = await gather(*tasks)
    met = []

    @sentry_span
    async def calculate_for_set_metrics(service, repos, for_sets):
        participants = [{
            rpk: getattr(with_, attr) or []
            for attr, rpk in (("releaser", ReleaseParticipationKind.RELEASER),
                              ("pr_author", ReleaseParticipationKind.PR_AUTHOR),
                              ("commit_author", ReleaseParticipationKind.COMMIT_AUTHOR))
        } for with_ in (filt.with_ or [])]
        release_metric_values, release_matches = await METRIC_ENTRIES[service]["releases_linear"](
            filt.metrics, time_intervals, filt.quantiles or (0, 1), repos, participants,
            JIRAFilter.from_web(filt.jira, jira_ids), release_settings,
            filt.account, meta_ids, request.mdb, request.pdb, request.rdb, request.cache)
        release_matches = {k: v.name for k, v in release_matches.items()}
        mrange = range(len(filt.metrics))
        for with_, repos_mvs in zip((filt.with_ or [None]), release_metric_values):
            for for_set, repo_group, granular_mvs in zip(for_sets, repos, repos_mvs):
                for granularity, ts, mvs in zip(filt.granularities, time_intervals, granular_mvs):
                    my_release_matches = {}
                    for r in repo_group:
                        r = PREFIXES[service] + r
                        try:
                            my_release_matches[r] = release_matches[r]
                        except KeyError:
                            continue
                    cm = CalculatedReleaseMetric(
                        for_=for_set,
                        with_=with_,
                        matches=my_release_matches,
                        metrics=filt.metrics,
                        granularity=granularity,
                        values=[CalculatedLinearMetricValues(
                            date=(d - tzoffset).date(),
                            values=[mvs[i][m].value for m in mrange],
                            confidence_mins=[mvs[i][m].confidence_min for m in mrange],
                            confidence_maxs=[mvs[i][m].confidence_max for m in mrange],
                            confidence_scores=[mvs[i][m].confidence_score() for m in mrange],
                        ) for i, d in enumerate(ts[:-1])])
                    for v in cm.values:
                        if sum(1 for c in v.confidence_scores if c is not None) == 0:
                            v.confidence_mins = None
                            v.confidence_maxs = None
                            v.confidence_scores = None
                    met.append(cm)

    tasks = []
    for service, repos in grouped_repos.items():
        tasks.append(calculate_for_set_metrics(service, repos, grouped_for_sets[service]))
    await gather(*tasks)
    return model_response(met)
