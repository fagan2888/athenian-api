import functools
import inspect
import logging
import pickle
import time
from typing import Any, ByteString, Callable, Coroutine, Optional, Tuple, Union

import aiomcache
from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.utils import INF
from xxhash import xxh64_hexdigest

from athenian.api import metadata
from athenian.api.metadata import __package__, __version__
from athenian.api.typing_utils import wraps

pickle.dumps = functools.partial(pickle.dumps, protocol=-1)
max_exptime = 30 * 24 * 3600  # 30 days according to the docs


def gen_cache_key(fmt: str, *args) -> bytes:
    """Compose a memcached-friendly cache key from a printf-like."""
    full_key = (fmt % args).encode()
    first_half = xxh64_hexdigest(full_key[:len(full_key) // 2])
    second_half = xxh64_hexdigest(full_key[len(full_key) // 2:])
    return (first_half + second_half).encode()


def cached(exptime: Union[int, Callable[..., int]],
           serialize: Callable[[Any], ByteString],
           deserialize: Callable[[ByteString], Any],
           key: Callable[..., Tuple],
           cache: Optional[Callable[..., Optional[aiomcache.Client]]] = None,
           refresh_on_access=False,
           ) -> Callable[[Callable[..., Coroutine]], Callable[..., Coroutine]]:
    """
    Return factory that creates decorators that cache function call results if possible.

    :param exptime: Cache item expiration time delta in seconds. Can be a callable the decorated \
                    function's arguments converted to **kwargs and joined with the function's \
                    call result as "result".
    :param serialize: Call result serializer.
    :param deserialize: Cached binary deserializer to the result type.
    :param key: Cache key selector. The decorated function's arguments are converted to **kwargs.
    :param cache: Cache client extractor. The decorated function's arguments are converted to \
                  **kwargs. If is None, the client is assigned to the function's "cache" argument.
    :param refresh_on_access: Reset the cache item's expiration period on each access.
    :return: Decorator that cache function call results if possible.
    """
    def wrapper_cached(func: Callable[..., Coroutine]) -> Callable[..., Coroutine]:
        """Decorate a function to return the cached result if possible."""
        log = logging.getLogger("%s.cache" % metadata.__package__)
        if exptime == max_exptime and not refresh_on_access:
            log.warning("%s will stay cached for max_exptime but will not refresh on access, "
                        "consider setting refresh_on_access=True", func.__name__)
        if cache is None:
            def discover_cache(**kwargs) -> Optional[aiomcache.Client]:
                try:
                    return kwargs["cache"]
                except KeyError:
                    raise AssertionError(
                        '"cache" is not one of %s arguments, you must explicitly define it: '
                        '@cached(cache=...)' % func.__qualname__)  # noqa: Q000
        elif callable(cache):
            discover_cache = cache
        else:
            def discover_cache(**kwargs):
                return cache
        signature = inspect.signature(func)

        # no functool.wraps() shit here! It discards the coroutine status and aiohttp notices that
        async def wrapped_cached(*args, **kwargs):
            start_time = time.time()
            args_dict = signature.bind(*args, **kwargs).arguments
            client = discover_cache(**args_dict)
            cache_key = full_name = None
            if client is not None:
                props = key(**args_dict)
                assert isinstance(props, tuple), "key() must return a tuple"
                full_name = func.__module__ + "." + func.__qualname__
                cache_key = gen_cache_key(full_name + "|" + "|".join([str(p) for p in props]))
                try:
                    buffer = await client.get(cache_key)
                except aiomcache.exceptions.ClientException:
                    log.exception("failed to fetch %s", cache_key)
                    buffer = None
                if buffer is not None:
                    result = deserialize(buffer)
                    t = exptime(result=result, **args_dict) if callable(exptime) else exptime
                    if refresh_on_access:
                        await client.touch(cache_key, t)
                    client.metrics["hits"].labels(__package__, __version__, full_name).inc()
                    client.metrics["hit_latency"] \
                        .labels(__package__, __version__, full_name) \
                        .observe(time.time() - start_time)
                    return result
            result = await func(*args, **kwargs)
            if client is not None:
                t = exptime(result=result, **args_dict) if callable(exptime) else exptime
                payload = serialize(result)
                try:
                    await client.set(cache_key, payload, exptime=t)
                except aiomcache.exceptions.ClientException:
                    log.exception("Failed to put %d bytes in memcached", len(payload))
                else:
                    client.metrics["misses"].labels(__package__, __version__, full_name).inc()
                    client.metrics["miss_latency"] \
                        .labels(__package__, __version__, full_name) \
                        .observe(time.time() - start_time)
                    client.metrics["size"] \
                        .labels(__package__, __version__, full_name) \
                        .observe(len(payload))
            return result

        return wraps(wrapped_cached, func)

    return wrapper_cached


def setup_cache_metrics(cache: Optional[aiomcache.Client], registry: CollectorRegistry):
    """Initialize the Prometheus metrics for tracking the cache interoperability."""
    if cache is None:
        return
    cache.metrics = {
        "hits": Counter(
            "cache_hits", "Number of times the cache was useful",
            ["app_name", "version", "func"],
            registry=registry,
        ),
        "misses": Counter(
            "cache_misses", "Number of times the cache was useless",
            ["app_name", "version", "func"],
            registry=registry,
        ),
        "hit_latency": Histogram(
            "cache_hit_latency", "Elapsed time to retrieve items from the cache",
            ["app_name", "version", "func"],
            registry=registry,
        ),
        "miss_latency": Histogram(
            "cache_miss_latency", "Elapsed time to retrieve items bypassing the cache",
            ["app_name", "version", "func"],
            buckets=[0.05, 0.1, 0.25, 0.5, 0.75, 1.0,
                     1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0,
                     12.0, 15.0, 20.0, 25.0, 30.0,
                     45.0, 60.0, 120.0, 180.0, 240.0, INF],
            registry=registry,
        ),
        "size": Histogram(
            "cache_size", "Cached object size",
            ["app_name", "version", "func"],
            buckets=[10, 100, 1000, 5000, 10000, 25000, 50000, 75000,
                     100000, 200000, 300000, 400000, 500000, 750000,
                     1000000, 2000000, 3000000, 4000000, 5000000, 7500000,
                     10000000, INF],
            registry=registry,
        ),
    }
