"""Retry utility decorator for resilient API calls."""

import asyncio
import logging
import time
from functools import wraps

logger = logging.getLogger(__name__)


def retry(max_attempts=3, delay=2, backoff=2, exceptions=(Exception,)):
    """API calls retry decorator."""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception: Exception = RuntimeError("Unknown error")
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts:
                        wait = delay * (backoff ** (attempt - 1))
                        logger.warning(
                            f"{func.__name__} attempt {attempt}/{max_attempts} failed: {e}. Retrying in {wait}s..."
                        )
                        # Use asyncio.sleep if in async context, else time.sleep
                        try:
                            loop = asyncio.get_running_loop()
                            loop.run_until_complete(asyncio.sleep(wait))
                        except RuntimeError:
                            time.sleep(wait)
                    else:
                        logger.error(
                            f"{func.__name__} FAILED after {max_attempts} attempts: {e}"
                        )
            raise last_exception

        return wrapper

    return decorator
