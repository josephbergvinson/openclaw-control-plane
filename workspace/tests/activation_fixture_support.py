"""Bind exported operational helpers to disposable fixture paths."""
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from scripts.operator_contract import OperatorContract


@contextmanager
def operator_binding(module, dotted_key: str, value):
    # Change the real input contract, not retention classification or mutation code.
    sections = ('paths', 'runtime', 'identifiers', 'maintenance')
    values = {name: module.OPERATOR.get(name, {}) for name in sections}
    section, key = dotted_key.split('.', 1)
    values[section][key] = str(value) if isinstance(value, Path) else value
    with mock.patch.object(module, 'OPERATOR', OperatorContract(values)):
        yield
