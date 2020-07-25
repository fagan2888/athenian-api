from contextvars import ContextVar
import logging
import os
import time

import aiohttp.web
import databases.core
from databases.interfaces import ConnectionBackend, TransactionBackend

from athenian.api import metadata
from athenian.api.typing_utils import wraps


profile_queries = os.getenv("PROFILE_QUERIES") in ("1", "true", "yes")


def measure_db_overhead(db: databases.Database,
                        db_id: str,
                        app: aiohttp.web.Application) -> databases.Database:
    """Instrument Database to measure the time spent inside DB i/o."""
    log = logging.getLogger("%s.measure_db_overhead" % metadata.__package__)
    _profile_queries = profile_queries

    def measure_method_overhead(func) -> callable:
        async def wrapped_measure_method_overhead(*args, **kwargs):
            start_time = time.time()
            try:
                return await func(*args, **kwargs)
            finally:
                elapsed = app["db_elapsed"].get()
                if elapsed is None:
                    log.warning("Cannot record the %s overhead", db_id)
                else:
                    delta = time.time() - start_time
                    elapsed[db_id] += delta
                    if _profile_queries:
                        sql = str(args[0]).replace("\n", "\\n").replace("\t", "\\t")
                        print("%f\t%s" % (delta, sql), flush=True)

        return wraps(wrapped_measure_method_overhead, func)

    backend_connection = db._backend.connection

    def wrapped_backend_connection() -> ConnectionBackend:
        connection = backend_connection()
        connection.fetch_all = measure_method_overhead(connection.fetch_all)
        connection.fetch_one = measure_method_overhead(connection.fetch_one)
        connection.execute = measure_method_overhead(connection.execute)
        connection.execute_many = measure_method_overhead(connection.execute_many)

        original_transaction = connection.transaction

        def transaction() -> TransactionBackend:
            t = original_transaction()
            t.start = measure_method_overhead(t.start)
            t.commit = measure_method_overhead(t.commit)
            t.rollback = measure_method_overhead(t.rollback)
            return t

        connection.transaction = transaction
        return connection

    db._backend.connection = wrapped_backend_connection
    return db


def add_pdb_metrics_context(app: aiohttp.web.Application) -> dict:
    """Create and attach the precomputed DB metrics context."""
    ctx = app["pdb_context"] = {
        "hits": ContextVar("pdb_hits", default=None),
        "misses": ContextVar("pdb_misses", default=None),
    }
    return ctx


pdb_metrics_logger = logging.getLogger("%s.pdb" % metadata.__package__)


def set_pdb_hits(pdb: databases.Database, topic: str, value: int) -> None:
    """Assign the `topic` precomputed DB hits to `value`."""
    pdb.metrics["hits"].get()[topic] = value
    pdb_metrics_logger.info("hits/%s: %d", topic, value)


def set_pdb_misses(pdb: databases.Database, topic: str, value: int) -> None:
    """Assign the `topic` precomputed DB misses to `value`."""
    pdb.metrics["misses"].get()[topic] = value
    pdb_metrics_logger.info("misses/%s: %d", topic, value)


def add_pdb_hits(pdb: databases.Database, topic: str, value: int) -> None:
    """Increase the `topic` precomputed hits by `value`."""
    if value < 0:
        pdb_metrics_logger.error('negative add_pdb_hits("%s", %d)', topic, value)
    pdb.metrics["hits"].get()[topic] += value
    pdb_metrics_logger.info("hits/%s: +%d", topic, value)


def add_pdb_misses(pdb: databases.Database, topic: str, value: int) -> None:
    """Increase the `topic` precomputed misses by `value`."""
    if value < 0:
        pdb_metrics_logger.error('negative add_pdb_misses("%s", %d)', topic, value)
    pdb.metrics["misses"].get()[topic] += value
    pdb_metrics_logger.info("misses/%s: +%d", topic, value)


class ParallelDatabase(databases.Database):
    """Override connection() to ignore the task context and spawn a new Connection every time."""

    def __str__(self):
        """Make Sentry debugging easier."""
        return "ParallelDatabase('%s', options=%s)" % (self.url, self.options)

    def connection(self) -> "databases.core.Connection":
        """Bypass self._connection_context."""
        return databases.core.Connection(self._backend)
