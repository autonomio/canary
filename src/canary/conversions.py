"""Reproduce recorded calculations with a small arithmetic language."""
import ast
import math
import operator
from decimal import Decimal, InvalidOperation

OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
             ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}
FUNCTIONS = {'sqrt': math.sqrt, 'log': math.log, 'log10': math.log10, 'exp': math.exp, 'abs': abs}
TOLERANCE = 0.5
MAX_LENGTH = 500


class Unsupported(ValueError):
    """The formula uses something outside the supported arithmetic."""


def evaluate(formula: str, inputs: dict[str, float]) -> float:
    """Evaluate arithmetic over named inputs; anything else is unsupported."""
    if not isinstance(formula, str) or not formula.strip() or len(formula) > MAX_LENGTH:
        raise Unsupported(f'Formula must be text of at most {MAX_LENGTH} characters')
    try:
        tree = ast.parse(formula, mode='eval')
    except SyntaxError as error:
        raise Unsupported('Formula is not arithmetic: ' + str(error.msg)) from None

    def value(node):
        if isinstance(node, ast.Expression):
            return value(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in inputs:
                raise Unsupported('Formula uses an unrecorded input: ' + node.id)
            return float(inputs[node.id])
        if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
            return OPERATORS[type(node.op)](value(node.left), value(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in OPERATORS:
            return OPERATORS[type(node.op)](value(node.operand))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS
                and len(node.args) == 1 and not node.keywords):
            return FUNCTIONS[node.func.id](value(node.args[0]))
        raise Unsupported('Formula uses unsupported syntax: ' + type(node).__name__)

    try:
        result = value(tree)
    except (ArithmeticError, ValueError, RecursionError) as error:
        if isinstance(error, Unsupported):
            raise
        raise Unsupported('Formula cannot be evaluated: ' + str(error)) from None
    if not isinstance(result, (int, float)) or isinstance(result, bool) or not math.isfinite(result):
        raise Unsupported('Formula does not give a finite number')
    return float(result)


def places(text: str) -> int | None:
    """The power of ten of a recorded number's last digit; None for anything but a finite number."""
    try:
        number = Decimal(str(text).strip())
    except InvalidOperation:
        return None
    return number.as_tuple().exponent if number.is_finite() else None


def reproduces(recorded: str, computed: float) -> bool:
    """The computed value rounds to the recorded value at its stated precision."""
    exponent = places(recorded)
    if exponent is None:
        return False
    return abs(computed - float(recorded)) <= TOLERANCE * 10.0 ** exponent * (1 + 1e-9)
