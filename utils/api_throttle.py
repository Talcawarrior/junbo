"""Global API throttle - tüm Open-Meteo istekleri için paylaşımlı.

MeteoFetcher ve WeatherEngine aynı throttle'ı kullanır.
Bu sayede rate limit sorunu çözülür.
"""

import time
import threading

# Global state - tüm modüller paylaşır
_last_request_time = 0.0
_lock = threading.Lock()
MIN_INTERVAL = 8.0  # Open-Meteo için 8 saniye (güvenli)


def throttle_open_meteo():
    """Open-Meteo API'si için global throttle.

    Tüm istekler bu fonksiyonu çağırmalı.
    Thread-safe: Aynı anda sadece bir istek gider.
    """
    global _last_request_time

    with _lock:
        now = time.monotonic()
        wait = MIN_INTERVAL - (now - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        _last_request_time = time.monotonic()


def get_throttle_status() -> dict:
    """Throttle durumunu göster."""
    with _lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        return {
            "last_request": _last_request_time,
            "elapsed": elapsed,
            "ready": elapsed >= MIN_INTERVAL,
        }
