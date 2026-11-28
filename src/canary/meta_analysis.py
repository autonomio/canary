"""Inverse-variance pooling that Canary runs itself to check a recorded statistical synthesis."""
from decimal import Decimal
import math

MODELS = ('inverse_variance_fixed', 'dersimonian_laird_random', 'other')
Z_95 = 1.959963984540054
REPORTED = {'inverse_variance_fixed': ('q', 'i2'), 'dersimonian_laird_random': ('q', 'i2', 'tau2')}
FIGURES = ('estimate', 'lower', 'upper', 'q', 'i2', 'tau2')


def usable(value):
    """A finite number that is not a boolean."""
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


def tolerance(value):
    """Half a unit in the last decimal place the figure was reported to."""
    exponent = Decimal(repr(value)).as_tuple().exponent
    return 0.5 * 10.0 ** exponent + 1e-9 if isinstance(exponent, int) else 1e-9


def differences(recorded, computed):
    """Each reported figure that does not round from Canary's own result at the precision it was reported."""
    return [{'field': name, 'recorded': recorded.get(name), 'recomputed': round(computed[name], 4)}
            for name in FIGURES
            if usable(recorded.get(name)) and abs(recorded[name] - computed[name]) > tolerance(recorded[name])]
