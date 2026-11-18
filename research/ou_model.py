"""Ornstein-Uhlenbeck gunluk-maksimum sicaklik modeli (ARASTIRMA, canli disi).

Kullanim disi karar: backtest faydali cikarsa ana koda port edilir, degilse
burada kalir. Canli yola import YOKTUR.

Model: mevsimsellikten arindirilmis anomali X_t icin
    X_{t+1} = a + b * X_t + eps  (AR(1) kestirimi)
    theta = -ln(b), mu = a / (1 - b)
    sigma: kalinti varyansindan
Hedef gun olasilik dagilimi (h gun sonrasi, kapali form):
    m = mu + (x0 - mu) * exp(-theta*h)
    v = sigma^2 / (2*theta) * (1 - exp(-2*theta*h))
Bucket olasiligi: Phi((T+1-m)/s) - Phi((T-m)/s)
"""

import math

try:
    from scipy.stats import norm as _norm

    def _phi(z: float) -> float:
        return float(_norm.cdf(z))
except Exception:  # pragma: no cover - scipy yoksa saf python

    def _phi(z: float) -> float:
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def fit_ar1(series: list[float]) -> dict | None:
    """AR(1) uydur: donus ORTA(3) nokta. None = yetersiz veri."""
    n = len(series)
    if n < 10:
        return None
    xs = series[:-1]
    ys = series[1:]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    b = max(0.01, min(0.99, b))
    a = my - b * mx
    resid = [y - (a + b * x) for x, y in zip(xs, ys)]
    var = sum(r * r for r in resid) / len(resid)
    theta = -math.log(b)
    mu = a / (1.0 - b)
    sigma = math.sqrt(max(var, 1e-6))
    return {"theta": theta, "mu": mu, "sigma": sigma, "n": n}


def predict_dist(params: dict, x0: float, horizon_days: int) -> tuple[float, float]:
    """(ortalama, std) h gun sonrasi icin."""
    theta, mu, sigma = params["theta"], params["mu"], params["sigma"]
    e1 = math.exp(-theta * horizon_days)
    m = mu + (x0 - mu) * e1
    v = sigma**2 / (2.0 * theta) * (1.0 - math.exp(-2.0 * theta * horizon_days))
    return m, math.sqrt(max(v, 1e-6))


def bucket_prob(params: dict, x0: float, horizon_days: int, bucket: int) -> float:
    """P(bucket <= T < bucket+1)."""
    m, s = predict_dist(params, x0, horizon_days)
    if s <= 0:
        return 0.0
    p = _phi((bucket + 1 - m) / s) - _phi((bucket - m) / s)
    return max(0.0, min(1.0, p))
