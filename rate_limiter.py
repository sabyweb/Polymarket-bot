"""
Rate-limited wrapper for the Polymarket CLOB client.

Enforces a minimum interval between API calls and retries with
exponential backoff on transient failures (429, 5xx, timeouts).
"""

import logging
import time
import threading

log = logging.getLogger(__name__)

# Minimum seconds between consecutive API calls
MIN_CALL_INTERVAL: float = 0.15  # ~6-7 calls/sec max

# Retry config
MAX_RETRIES: int = 3
BASE_BACKOFF: float = 1.0  # seconds


class RateLimitedClient:
    """Wraps a ClobClient to add rate limiting and retry logic.

    All public methods of the underlying client are accessible via
    attribute delegation.  Methods that hit the API are intercepted
    to enforce rate limits.
    """

    # Methods that make API calls and should be rate-limited
    _RATE_LIMITED_METHODS = {
        "get_order_book", "get_orders", "get_order",
        "create_and_post_order", "cancel",
        "get_balance_allowance", "update_balance_allowance",
    }

    def __init__(self, client: object) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._last_call: float = 0.0

    def __getattr__(self, name: str):
        attr = getattr(self._client, name)
        if name in self._RATE_LIMITED_METHODS and callable(attr):
            return self._make_rate_limited(name, attr)
        return attr

    def _make_rate_limited(self, name: str, method):
        def wrapper(*args, **kwargs):
            return self._call_with_retry(name, method, *args, **kwargs)
        return wrapper

    def _throttle(self) -> None:
        """Enforce minimum interval between API calls."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < MIN_CALL_INTERVAL:
                time.sleep(MIN_CALL_INTERVAL - elapsed)
            self._last_call = time.monotonic()

    def _call_with_retry(self, name: str, method, *args, **kwargs):
        """Execute an API call with rate limiting and exponential backoff."""
        last_error = None

        for attempt in range(MAX_RETRIES + 1):
            self._throttle()
            try:
                result = method(*args, **kwargs)
                return result
            except Exception as e:
                last_error = e
                error_str = str(e).lower()

                # Check for rate limit or transient server errors
                is_rate_limit = "429" in error_str or "rate" in error_str
                is_server_error = any(
                    code in error_str for code in ("500", "502", "503", "504")
                )
                is_timeout = "timeout" in error_str or "timed out" in error_str

                if not (is_rate_limit or is_server_error or is_timeout):
                    # Not a transient error — don't retry
                    raise

                if attempt < MAX_RETRIES:
                    backoff = BASE_BACKOFF * (2 ** attempt)
                    if is_rate_limit:
                        backoff *= 2  # Extra backoff for rate limits
                    log.warning(
                        f"API call {name} failed (attempt {attempt + 1}/"
                        f"{MAX_RETRIES + 1}): {e} — retrying in {backoff:.1f}s"
                    )
                    time.sleep(backoff)
                else:
                    log.error(
                        f"API call {name} failed after {MAX_RETRIES + 1} "
                        f"attempts: {e}"
                    )

        raise last_error
