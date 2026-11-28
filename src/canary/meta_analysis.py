"""Inverse-variance pooling that Canary runs itself to check a recorded statistical synthesis."""
import math

MODELS = ('inverse_variance_fixed', 'dersimonian_laird_random', 'other')
Z_95 = 1.959963984540054
REPORTED = {'inverse_variance_fixed': ('q', 'i2'), 'dersimonian_laird_random': ('q', 'i2', 'tau2')}
TOLERANCE = {'estimate': 0.01, 'lower': 0.01, 'upper': 0.01, 'q': 0.01, 'i2': 0.5, 'tau2': 0.01}


def usable(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def pool(inputs, model):
    """Pooled estimate, 95% normal interval, Q, I² (percent) and τ² for (estimate, standard error) pairs."""
    if model not in MODELS[:2] or not inputs:
        return None
    weights = [1 / se ** 2 for _, se in inputs]
    total = sum(weights)
    fixed = sum(w * y for w, (y, _) in zip(weights, inputs)) / total
    q = sum(w * (y - fixed) ** 2 for w, (y, _) in zip(weights, inputs))
    df = len(inputs) - 1
    i2 = max(0.0, (q - df) / q * 100) if q > 0 else 0.0
    spread = total - sum(w ** 2 for w in weights) / total
    tau2 = max(0.0, (q - df) / spread) if spread > 0 else 0.0
    if model == 'dersimonian_laird_random':
        weights = [1 / (se ** 2 + tau2) for _, se in inputs]
        total = sum(weights)
    estimate = sum(w * y for w, (y, _) in zip(weights, inputs)) / total
    half = Z_95 * math.sqrt(1 / total)
    return {'estimate': estimate, 'lower': estimate - half, 'upper': estimate + half, 'q': q, 'i2': i2, 'tau2': tau2}


def differences(recorded, computed):
    """Each reported figure that is outside the tolerance of Canary's own result."""
    return [{'field': name, 'recorded': recorded.get(name), 'recomputed': round(computed[name], 4)}
            for name, tolerance in TOLERANCE.items()
            if usable(recorded.get(name)) and abs(recorded[name] - computed[name]) > tolerance]
