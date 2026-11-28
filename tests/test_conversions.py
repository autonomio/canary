import pytest

from canary.conversions import Unsupported, evaluate, reproduces


def test_arithmetic_over_named_inputs():
    assert evaluate('mean * 9.81 / 1000', {'mean': 12.5}) == pytest.approx(0.122625)
    assert evaluate('(upper - lower) / (2 * 1.96)', {'upper': 3.2, 'lower': 1.0}) == pytest.approx(0.5612244898)
    assert evaluate('sqrt(n) + log10(x)', {'n': 16, 'x': 100}) == 6.0


@pytest.mark.parametrize('formula', ['__import__("os")', 'a.b', 'missing', '[1]', 'x if x else 1', '', 'x' * 501,
                                     '1/0', '10**400', '10**10**10', 'sqrt(-1)', '(-8)**0.5', 'log(1, 2)'])
def test_anything_outside_finite_arithmetic_is_unsupported(formula):
    with pytest.raises(Unsupported):
        evaluate(formula, {'x': 1.0})


def test_a_value_is_reproduced_at_its_recorded_precision():
    assert reproduces('0.12', 0.122625) and reproduces('0.1226', 0.122625)
    assert not reproduces('0.13', 0.122625) and not reproduces('about 0.12', 0.122625)
    assert reproduces('123', 122.6) and not reproduces('123', 122.4)
    assert reproduces('1e-3', 0.0012) and not reproduces('1e-3', 0.4) and not reproduces('1e-3', 0.0016)
    assert reproduces('1.2E3', 1240) and not reproduces('1.2E3', 1260)
    assert not reproduces('nan', 1.0) and not reproduces('inf', 1e308)
