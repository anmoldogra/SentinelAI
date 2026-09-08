"""RFC 8785 JCS canonical encoding — ADR-0003 §2, modernization Wave 1.1.

Three kinds of proof, because each catches a different class of defect:

* **Specification vectors** (RFC 8785 §3.2.3 and ECMAScript ``Number::toString``) prove we
  implement *the standard*, not merely something self-consistent. A hand-rolled encoder that is
  deterministic but non-conformant would pass every property test below and still produce
  evidence no independent verifier could reproduce.
* **Property tests** over a seeded, randomly-generated corpus prove determinism holds across
  shapes nobody thought to write down.
* **Divergence tests** pin the specific ways ``json.dumps(sort_keys=True)`` — the encoding the
  custody and audit ledgers use today — differs from JCS. Those are the defects Wave 1.2 exists
  to fix; pinning them here is what makes that increment's scope concrete.

The seeded generator is deliberate rather than a ``hypothesis`` dependency: for a *determinism*
property, a fixed corpus identical on every machine and in every CI run is worth more than random
exploration, and it keeps the air-gapped dependency surface unchanged.

Control characters are written as ``chr(...)`` throughout rather than as literals, so this file
stays reviewable ASCII and a stray invisible byte can never masquerade as a passing assertion.
"""

from __future__ import annotations

import json
import random
from typing import Any

import pytest

from sentinelai.platform.crypto.canonical import (
    CANONICAL_ENCODING,
    CanonicalizationError,
    canonicalize,
)

_NUL = chr(0x00)
_UNIT_SEPARATOR = chr(0x1F)
# U+0080 is a C1 control, but JCS escapes only code points below U+0020 — so it must appear in
# the output as literal UTF-8 (0xC2 0x80), not as an escape. That is what the RFC vector checks.
_PAD = chr(0x80)

# --------------------------------------------------------------------------------------
# Specification vectors
# --------------------------------------------------------------------------------------

# RFC 8785 §3.2.3's worked example. The discriminating entry is the emoji: U+1F600 is the
# surrogate pair D83D/DE00 in UTF-16, so it sorts BEFORE U+FB33 by code unit — and AFTER it by
# code point, which is what Python's default ``sorted()`` gives. An implementation that sorts
# keys with a plain ``sorted()`` fails exactly here and nowhere else.
_RFC_8785_SORTING_INPUT: dict[str, str] = {
    "€": "Euro Sign",
    "\r": "Carriage Return",
    "דּ": "Hebrew Letter Dalet With Dagesh",
    "1": "One",
    "\U0001f600": "Emoji: Grinning Face",
    _PAD: "Control",
    "ö": "Latin Small Letter O With Diaeresis",
}

_RFC_8785_SORTING_EXPECTED = (
    '{"\\r":"Carriage Return",'
    '"1":"One",'
    '"' + _PAD + '":"Control",'
    '"ö":"Latin Small Letter O With Diaeresis",'
    '"€":"Euro Sign",'
    '"\U0001f600":"Emoji: Grinning Face",'
    '"דּ":"Hebrew Letter Dalet With Dagesh"}'
)


def test_rfc_8785_sorting_vector() -> None:
    assert canonicalize(_RFC_8785_SORTING_INPUT).decode("utf-8") == _RFC_8785_SORTING_EXPECTED


def test_key_order_is_utf16_code_unit_not_code_point() -> None:
    """The property the vector above depends on, asserted directly so a failure is legible."""
    keys = list(_RFC_8785_SORTING_INPUT)
    by_code_point = sorted(keys)
    by_code_unit = sorted(keys, key=lambda k: k.encode("utf-16-be"))
    # If these ever stop differing, this test has stopped proving anything.
    assert by_code_point != by_code_unit

    emitted = list(json.loads(canonicalize(_RFC_8785_SORTING_INPUT).decode("utf-8")))
    assert emitted == by_code_unit
    assert emitted != by_code_point


# ECMAScript Number::toString (ECMA-262 §6.1.6.1.20), which RFC 8785 §3.2.2.3 adopts verbatim.
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "0"),
        (-0.0, "0"),  # negative zero renders as "0"
        (1.0, "1"),  # NOT "1.0" — the json.dumps divergence
        (-1.0, "-1"),
        (1.5, "1.5"),
        (100.0, "100"),
        (0.1, "0.1"),
        (0.001, "0.001"),
        (1e-5, "0.00001"),
        (1e-6, "0.000001"),
        (1e-7, "1e-7"),  # NOT "1e-07" — the exponent is never zero-padded
        (1.5e-8, "1.5e-8"),
        (1e-10, "1e-10"),
        (1e20, "100000000000000000000"),  # fixed-point right up to n == 21
        (1e21, "1e+21"),  # and exponential from n == 22
        (1e22, "1e+22"),
        (123.456, "123.456"),
        (9007199254740991.0, "9007199254740991"),  # 2**53 - 1
        (5e-324, "5e-324"),  # smallest subnormal
        (1.7976931348623157e308, "1.7976931348623157e+308"),  # largest finite double
    ],
)
def test_ecmascript_number_rendering(value: float, expected: str) -> None:
    assert canonicalize({"n": value}).decode("utf-8") == '{"n":' + expected + "}"


def test_integers_render_without_a_decimal_point() -> None:
    assert canonicalize([0, 1, -1, 42, 9007199254740991]).decode("utf-8") == (
        "[0,1,-1,42,9007199254740991]"
    )


def test_int_and_equal_float_canonicalize_identically() -> None:
    """``1`` and ``1.0`` are one JSON number, so they must produce one preimage.

    This matters because JSONB stores both as ``numeric`` and a round-trip can hand back either.
    """
    assert canonicalize({"n": 1}) == canonicalize({"n": 1.0})


def test_string_escaping_is_minimal_and_utf8_literal() -> None:
    value = "a" + _NUL + 'b"c\\d\be\tf\ng\fh\rié\U0001f600"'
    expected = '{"k":"a\\u0000b\\"c\\\\d\\be\\tf\\ng\\fh\\rié\U0001f600\\""}'
    assert canonicalize({"k": value}).decode("utf-8") == expected


def test_control_character_escapes_are_lowercase_hex() -> None:
    """JSON.stringify — which JCS defers to — emits ``\\u001f``, never ``\\u001F``."""
    assert canonicalize({"k": _UNIT_SEPARATOR}).decode("utf-8") == '{"k":"\\u001f"}'


def test_c1_controls_above_u001f_are_emitted_literally() -> None:
    """JCS escapes only below U+0020; U+0080 goes out as its two UTF-8 bytes."""
    assert canonicalize({"k": _PAD}) == b'{"k":"\xc2\x80"}'


def test_non_ascii_is_never_backslash_escaped() -> None:
    """The ``ensure_ascii=True`` divergence: JCS emits real UTF-8 bytes."""
    encoded = canonicalize({"k": "é€\U0001f600"})
    assert b"\\u" not in encoded
    assert encoded.decode("utf-8") == '{"k":"é€\U0001f600"}'


def test_literals_and_structure() -> None:
    assert canonicalize({"t": True, "f": False, "n": None, "a": [1, 2.0, "x"], "o": {}}) == (
        b'{"a":[1,2,"x"],"f":false,"n":null,"o":{},"t":true}'
    )


def test_booleans_are_not_rendered_as_integers() -> None:
    """``bool`` subclasses ``int`` in Python; an unguarded int branch would emit ``1``/``0``."""
    assert canonicalize([True, False]) == b"[true,false]"
    assert canonicalize([1, 0]) == b"[1,0]"


def test_empty_containers() -> None:
    assert canonicalize({}) == b"{}"
    assert canonicalize([]) == b"[]"


def test_tuples_canonicalize_as_arrays() -> None:
    assert canonicalize((1, 2)) == canonicalize([1, 2]) == b"[1,2]"


def test_canonical_encoding_identifier_is_stable() -> None:
    """Persisted next to every hash; changing it silently would orphan historical entries."""
    assert CANONICAL_ENCODING == "RFC8785/JCS"


# --------------------------------------------------------------------------------------
# Divergence from the encoding the ledgers use today
# --------------------------------------------------------------------------------------


def _legacy(value: Any) -> bytes:
    """The encoding ``_custody_entry_hash``/``_compute_hash`` use today (Wave 1.2 replaces it)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ({"n": 1.0}, "an integral float renders as 1.0, not 1"),
        ({"n": 1e-7}, "the exponent is zero-padded to 1e-07"),
        ({"k": "é"}, "ensure_ascii escapes non-ASCII"),
        ({"\U0001f600": 1, "דּ": 2}, "keys sort by code point, not UTF-16 code unit"),
    ],
)
def test_jcs_differs_from_the_current_ledger_encoding(value: Any, reason: str) -> None:
    """Pins *why* Wave 1.1 is a prerequisite: these inputs hash differently under the two."""
    assert canonicalize(value) != _legacy(value), reason


# --------------------------------------------------------------------------------------
# Property tests — determinism over a seeded corpus
# --------------------------------------------------------------------------------------

_ALPHABET = "abc XYZ_0123-é€דּ\U0001f600\r\n\t" + '"' + "\\" + _NUL


def _random_json(rng: random.Random, depth: int = 0) -> Any:
    """A random JSON value. Bounded depth; leaf-only past depth 4 so corpora stay finite."""
    leaf_only = depth >= 4
    kind = rng.choice(
        ["null", "bool", "int", "float", "str"]
        if leaf_only
        else ["null", "bool", "int", "float", "str", "list", "dict", "dict", "list"]
    )
    if kind == "null":
        return None
    if kind == "bool":
        return rng.choice([True, False])
    if kind == "int":
        return rng.randint(-(2**53) + 1, 2**53 - 1)
    if kind == "float":
        return rng.choice(
            [
                rng.uniform(-1e6, 1e6),
                rng.uniform(-1, 1),
                float(rng.randint(-1000, 1000)),
                rng.choice([1e-7, 1e21, 0.1, 5e-324, 1.7976931348623157e308, 0.0, -0.0]),
            ]
        )
    if kind == "str":
        return "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 12)))
    if kind == "list":
        return [_random_json(rng, depth + 1) for _ in range(rng.randint(0, 5))]
    return {
        "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(1, 8))): _random_json(
            rng, depth + 1
        )
        for _ in range(rng.randint(0, 5))
    }


def _shuffled_copy(value: Any, rng: random.Random) -> Any:
    """Structurally equal, but every object's key insertion order is randomized."""
    if isinstance(value, dict):
        items = [(k, _shuffled_copy(v, rng)) for k, v in value.items()]
        rng.shuffle(items)
        return dict(items)
    if isinstance(value, list):
        return [_shuffled_copy(item, rng) for item in value]
    return value


_CORPUS = [_random_json(random.Random(seed)) for seed in range(1500)]


def test_property_corpus_is_actually_varied() -> None:
    """Guards the guard: a generator that silently produced 1500 nulls would pass everything.

    Exact uniqueness is not the bar and never could be — a scalar leaf like ``null`` or ``true``
    legitimately recurs across seeds. What matters is that every JSON type is represented and the
    corpus is overwhelmingly distinct, so the properties below are exercised on real variety.
    """
    assert len({canonicalize(v) for v in _CORPUS}) > 900

    kinds = set()

    def collect(value: Any) -> None:
        kinds.add(type(value).__name__)
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for value in _CORPUS:
        collect(value)
    assert {"NoneType", "bool", "int", "float", "str", "list", "dict"} <= kinds


def test_property_encoding_is_idempotent() -> None:
    """Same value, same bytes — every time."""
    for value in _CORPUS:
        assert canonicalize(value) == canonicalize(value)


def test_property_key_insertion_order_is_irrelevant() -> None:
    """The core JCS guarantee, and the one JSONB round-trips actually stress."""
    rng = random.Random(99)
    for value in _CORPUS:
        assert canonicalize(value) == canonicalize(_shuffled_copy(value, rng))


def test_property_output_is_valid_utf8_json_that_reparses_equal() -> None:
    """Canonical bytes must still be JSON, and must still mean the same thing."""
    for value in _CORPUS:
        encoded = canonicalize(value)
        assert json.loads(encoded.decode("utf-8")) == json.loads(json.dumps(value))


def test_property_canonicalizing_a_reparse_is_a_fixed_point() -> None:
    """encode -> parse -> encode == encode. Without this, a round-trip could drift each hop."""
    for value in _CORPUS:
        once = canonicalize(value)
        assert canonicalize(json.loads(once.decode("utf-8"))) == once


# --------------------------------------------------------------------------------------
# Rejections — every one is a case where coercing would silently change a preimage
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_rejects_non_json_numbers(value: float) -> None:
    with pytest.raises(CanonicalizationError):
        canonicalize({"n": value})


@pytest.mark.parametrize("value", [2**53 + 1, 2**53 + 3, -(2**53) - 1, 10**400])
def test_rejects_integers_that_are_not_exactly_representable(value: int) -> None:
    """2**53 + 1 collapses onto the same double as 2**53, so it has no canonical JCS form."""
    with pytest.raises(CanonicalizationError, match="IEEE-754"):
        canonicalize({"n": value})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (2**53, "9007199254740992"),  # a power of two — exact, despite exceeding 2**53 - 1
        (10**21, "1e+21"),  # 5**21 fits in 53 bits, so this is exact
        (10**22, "1e+22"),
        # Exactly representable, but ECMAScript prints the *shortest* decimal that round-trips
        # to that double — so 2**62 renders as ...388000, not its full digit expansion. This is
        # correct JCS output, and it is precisely why the encoding must be pinned by a version.
        (2**62, "4611686018427388000"),
    ],
)
def test_accepts_large_integers_that_are_exactly_representable(value: int, expected: str) -> None:
    """Magnitude is not the criterion — exact double representability is.

    This is the bound a real PostgreSQL round-trip exposed: ``jsonb`` renders a stored 1e21 back
    as the digit string ``1000000000000000000000``, which ``json.loads`` yields as an ``int``. A
    magnitude-based guard would reject on the way out what it accepted on the way in.
    """
    assert canonicalize({"n": value}).decode("utf-8") == '{"n":' + expected + "}"


def test_large_int_and_its_float_form_agree() -> None:
    """The round-trip identity that matters: 1e21 in, 10**21 out, one preimage."""
    assert canonicalize({"n": 10**21}) == canonicalize({"n": 1e21})


def test_rejects_non_string_object_keys() -> None:
    with pytest.raises(CanonicalizationError, match="keys must be strings"):
        canonicalize({1: "x"})


def test_rejects_non_json_types() -> None:
    with pytest.raises(CanonicalizationError, match="not a JSON type"):
        canonicalize({"k": {1, 2}})


def test_rejects_unpaired_surrogates() -> None:
    with pytest.raises(CanonicalizationError, match="surrogate"):
        canonicalize({"k": "\ud800"})
    with pytest.raises(CanonicalizationError, match="surrogate"):
        canonicalize({"\ud800": "k"})


def test_rejects_excessive_nesting_rather_than_overflowing_the_stack() -> None:
    deep: Any = "leaf"
    for _ in range(150):
        deep = [deep]
    with pytest.raises(CanonicalizationError, match="nested deeper"):
        canonicalize(deep)
