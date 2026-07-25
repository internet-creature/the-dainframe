"""Script creation validates per-line obligations (DESIGN.md §5.2): illegal
response/delivery combos encode contradictory intent, and an empty script
needs an explicit noop_reason - a broken Director must not silently drop an
ordinary user turn."""

import pytest

from dainframe.core.types import Script, ScriptLine, ScriptValidationError


def test_legal_combinations_construct():
    Script(lines=(
        ScriptLine(speaker="a", response="required", delivery="direct"),
        ScriptLine(speaker="b", response="required", delivery="pending"),
    ))
    Script(lines=(
        ScriptLine(speaker="c", response="optional", delivery="direct"),
        ScriptLine(speaker="d", response="optional", delivery="pending"),
        ScriptLine(speaker="e", response="silent", delivery="none"),
    ))


@pytest.mark.parametrize(
    ("response", "delivery"),
    [
        ("silent", "direct"),    # silent text has nowhere legitimate to go
        ("silent", "pending"),
        ("required", "none"),    # required text must say where it goes
        ("optional", "none"),
    ],
)
def test_contradictory_combinations_fail_at_creation(response, delivery):
    with pytest.raises(ScriptValidationError):
        Script(lines=(ScriptLine(speaker="x", response=response, delivery=delivery),))


def test_empty_script_requires_an_explicit_noop_reason():
    with pytest.raises(ScriptValidationError):
        Script()
    assert Script(noop_reason="no rule for this kind").noop_reason
