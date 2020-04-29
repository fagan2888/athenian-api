import argparse
import asyncio
from datetime import date, datetime, timedelta, timezone
import logging

import databases
from sqlalchemy import and_, create_engine
from sqlalchemy.orm import Session, sessionmaker
from tqdm import tqdm

from athenian.api import add_logging_args, create_memcached, setup_cache_metrics
from athenian.api.controllers.features.entries import calc_pull_request_metrics_line_github
from athenian.api.controllers.settings import default_branch_alias, Match, ReleaseMatchSetting
from athenian.api.models.state.models import ReleaseSetting, RepositorySet


def parse_args():
    """Go away linter."""
    parser = argparse.ArgumentParser()
    add_logging_args(parser)
    parser.add_argument("--metadata-db", required=True,
                        help="Metadata DB endpoint, e.g. postgresql://0.0.0.0:5432/state")
    parser.add_argument("--state-db", required=True,
                        help="State DB endpoint, e.g. postgresql://0.0.0.0:5432/state")
    parser.add_argument("--memcached", required=True,
                        help="memcached address, e.g. 0.0.0.0:11211")
    return parser.parse_args()


def main():
    """Go away linter."""
    log = logging.getLogger("heat_cache")
    args = parse_args()
    engine = create_engine(args.state_db)
    session = sessionmaker(bind=engine)()  # type: Session
    reposets = session.query(RepositorySet).all()
    time_to = datetime.combine(date.today() + timedelta(days=1),
                               datetime.min.time(),
                               tzinfo=timezone.utc)
    time_from = time_to - timedelta(days=30)

    async def async_run():
        cache = create_memcached(args.memcached, log)
        setup_cache_metrics(cache, None)
        mdb = databases.Database(args.metadata_db)
        await mdb.connect()

        for reposet in tqdm(reposets):
            repos = [r.split("/", 1)[1] for r in reposet.items]
            settings = {}
            rows = session.query(ReleaseSetting).filter(and_(
                ReleaseSetting.account_id == reposet.owner,
                ReleaseSetting.repository.in_(reposet.items)))
            for row in rows:
                settings[row.repository] = ReleaseMatchSetting(
                    branches=row.branches,
                    tags=row.tags,
                    match=Match(row.match),
                )
            for repo in reposet.items:
                if repo not in settings:
                    settings[repo] = ReleaseMatchSetting(
                        branches=default_branch_alias,
                        tags=".*",
                        match=Match.tag_or_branch,
                    )
            await calc_pull_request_metrics_line_github(
                ["pr-lead-time"],
                [[time_from, time_to]],
                repos,
                settings,
                [],
                mdb,
                cache,
            )

    asyncio.run(async_run())


if __name__ == "__main__":
    exit(main())