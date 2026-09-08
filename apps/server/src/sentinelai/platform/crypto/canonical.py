"""RFC 8785 JSON Canonicalization Scheme (JCS) — ADR-0003 §2, modernization Wave 1.1.

The deterministic serialization primitive every evidentiary preimage is built on. Given two
structurally equal JSON values, this produces **byte-identical** output regardless of key
insertion order, whitespace, or how the value reached us — in particular, regardless of a
PostgreSQL ``JSONB`` round-trip, which reorders object keys and normalizes numbers by design.

Why this exists rather than ``json.dumps(sort_keys=True, separators=(",", ":"))``, which is what
``ingestion.service._custody_entry_hash`` and ``platform.auth.audit._compute_hash`` use today:
that encoding is deterministic *for a fixed Python process*, but it is not RFC 8785 and it is not
stable across the things an evidentiary chain must survive. It differs in three ways that each
silently change the hash:

1. **Number formatting.** ``json.dumps`` emits ``repr(float)`` — ``1.0``, ``1e-07``,
   ``1.0000000000000002e+22``. JCS mandates ECMAScript ``Number::toString`` — ``1``, ``1e-7``,
   ``1.0000000000000002e+22``. A float that survives a JSONB round-trip as the same IEEE-754
   double would hash differently before and after.
2. **Non-ASCII escaping.** ``json.dumps`` defaults to ``ensure_ascii=True`` and emits
   ``\\u00e9``; JCS emits the literal UTF-8 bytes. Any evidence note in a non-English script
   hashes differently depending on which encoder ran.
3. **Key ordering.** Python sorts ``str`` by code point; JCS §3.2.3 sorts by **UTF-16 code
   unit**. These disagree whenever a key contains a character above U+FFFF (a surrogate pair
   sorts below U+E000-U+FFFF, but its code point sorts above). Rare, but it is exactly the
   "works until it doesn't" defect a 15-year evidentiary format cannot carry.

This module deliberately does **not** hash anything and does **not** know what a custody entry or
an audit entry looks like. It turns a JSON value into canonical bytes; ADR-0003 §1's preimage
construction (which fields, in what order, chained to what) is Wave 1.2's job and lives with the
ledgers themselves. Keeping the two apart is what lets ``preimage_version`` and the encoding
version move independently.

Air-gap note: pure stdlib, no network, no new dependency.
"""

from __future__ import annotations

import math
from typing import Final

from sentinelai.platform.crypto.exceptions import CryptoError

# The encoding this module implements, recorded alongside every hash it feeds so a verifier can
# dispatch on it after the format evolves. This names the *encoding*; `preimage_version` on the
# ledger tables names the *field set*, which is a separate axis (ADR-0003 §5).
CANONICAL_ENCODING: Final = "RFC8785/JCS"

# JSON's structural characters and the two-character escapes RFC 8785 §3.2.2.2 requires. Every
# other control character below 0x20 becomes \u00xx with **lowercase** hex, matching
# ECMAScript's JSON.stringify, which JCS defers to.
_SHORT_ESCAPES: Final[dict[int, str]] = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


class CanonicalizationError(CryptoError):
    """A value cannot be canonicalized under RFC 8785.

    Always raised rather than coerced. A silent coercion here would change an evidentiary
    preimage without changing anything visible, which is the one failure mode this subsystem
    exists to prevent.
    """


def _es_number_to_string(value: float) -> str:
    """ECMAScript ``Number::toString`` (ECMA-262 §6.1.6.1.20), required by RFC 8785 §3.2.2.3.

    Python's ``repr`` already yields the *shortest* decimal that round-trips to the same double —
    the same digit string ECMAScript's step 5 selects — so the digits are taken from ``repr`` and
    only the **layout** (fixed-point vs exponential, and where the point goes) is re-derived to
    ECMAScript's rules. That keeps this exact without reimplementing shortest-float printing.
    """
    if math.isnan(value) or math.isinf(value):
        # JSON has no NaN/Infinity; json.dumps emits them as bare tokens, which is invalid JSON
        # and would produce a preimage no other implementation could reproduce.
        raise CanonicalizationError(f"{value!r} is not representable in JSON")
    if value == 0:  # also normalizes -0.0, which ECMAScript renders as "0"
        return "0"
    if value < 0:
        return "-" + _es_number_to_string(-value)

    text = repr(value)
    mantissa, _, exponent_text = text.partition("e")
    exponent = int(exponent_text) if exponent_text else 0
    integer_part, _, fraction_part = mantissa.partition(".")

    # Reduce to ECMAScript's (s, k, n): s has k digits, no leading or trailing zeros, and
    # value == s * 10**(n - k).
    all_digits = integer_part + fraction_part
    without_leading = all_digits.lstrip("0")
    digits = without_leading.rstrip("0")
    trailing_zeros = len(without_leading) - len(digits)
    k = len(digits)
    n = k + exponent - len(fraction_part) + trailing_zeros

    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return "0." + "0" * -n + digits
    # Exponential form. ECMAScript writes the exponent sign explicitly and never zero-pads it,
    # so 1e-7 is "1e-7" (Python's repr gives "1e-07").
    exponent_suffix = f"e{'+' if n - 1 >= 0 else '-'}{abs(n - 1)}"
    return (digits if k == 1 else digits[0] + "." + digits[1:]) + exponent_suffix


def _serialize_number(value: int | float) -> str:
    """Render a JSON number per JCS, rejecting anything IEEE-754 cannot carry exactly.

    RFC 8785 §3.2.2.3 defines numbers as IEEE-754 doubles, so the question for a Python ``int`` is
    not its magnitude but whether it survives the conversion: an int is admissible exactly when
    ``float(n)`` round-trips back to ``n``. That admits large-but-exact values such as 10**21
    (= 5**21 * 2**21, and 5**21 fits in 53 bits) while still rejecting 2**53 + 1, which collapses
    onto the same double as 2**53.

    Getting this bound right matters in practice, not just in theory: PostgreSQL stores JSON
    numbers as ``numeric`` and renders 1e21 back as the digit string ``1000000000000000000000``,
    which ``json.loads`` yields as an ``int``. A naive ``abs(n) <= 2**53 - 1`` guard would reject a
    value on the way *out* of the database that it had happily accepted on the way *in* — the
    exact round-trip divergence this module exists to prevent.

    Rejection (rather than rounding) is the only safe answer for the genuinely inexact case:
    rounding would let two distinct evidentiary values produce one identical hash.
    """
    if isinstance(value, int):
        try:
            as_double = float(value)
        except OverflowError:  # beyond the double range entirely
            as_double = math.inf
        if not math.isinf(as_double) and int(as_double) == value:
            return _es_number_to_string(as_double)
        raise CanonicalizationError(
            f"integer {value} is not exactly representable as an IEEE-754 double "
            "and therefore has no canonical JCS form"
        )
    return _es_number_to_string(value)


def _serialize_string(value: str) -> str:
    """Escape per RFC 8785 §3.2.2.2: minimal escaping, literal UTF-8 for everything else."""
    pieces = ['"']
    for character in value:
        code_point = ord(character)
        short = _SHORT_ESCAPES.get(code_point)
        if short is not None:
            pieces.append(short)
        elif code_point < 0x20:
            pieces.append(f"\\u{code_point:04x}")
        elif 0xD800 <= code_point <= 0xDFFF:
            # A lone surrogate cannot be encoded as UTF-8. Python allows one in a str; JSON does
            # not, and encoding would raise far from here with an opaque message.
            raise CanonicalizationError(
                f"unpaired surrogate U+{code_point:04X} cannot be canonicalized"
            )
        else:
            pieces.append(character)
    pieces.append('"')
    return "".join(pieces)


def _serialize(value: object, depth: int) -> str:
    if depth > 100:
        # Bounded so a hostile or accidentally cyclic structure fails fast with a clear error
        # rather than exhausting the C stack inside a request.
        raise CanonicalizationError("structure nested deeper than 100 levels")

    if value is None:
        return "null"
    # bool must precede int: in Python, bool is a subclass of int, so isinstance(True, int) is
    # True and an unguarded int branch would render True as "1".
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _serialize_string(value)
    if isinstance(value, int | float):
        return _serialize_number(value)
    if isinstance(value, list | tuple):
        return "[" + ",".join(_serialize(item, depth + 1) for item in value) + "]"
    if isinstance(value, dict):
        members = []
        for key in _sorted_keys(value):
            members.append(_serialize_string(key) + ":" + _serialize(value[key], depth + 1))
        return "{" + ",".join(members) + "}"
    raise CanonicalizationError(f"{type(value).__name__} is not a JSON type")


def _sorted_keys(mapping: dict[object, object]) -> list[str]:
    """Object keys in RFC 8785 §3.2.3 order: ascending by **UTF-16 code unit**.

    Encoding to UTF-16BE and comparing the resulting bytes is exactly code-unit order, because
    big-endian byte comparison of 16-bit units orders them as unsigned integers. Python's default
    ``str`` ordering is by code point, which differs above U+FFFF.
    """
    keys = []
    for key in mapping:
        if not isinstance(key, str):
            raise CanonicalizationError(
                f"JSON object keys must be strings, got {type(key).__name__}"
            )
        keys.append(key)
    try:
        return sorted(keys, key=lambda k: k.encode("utf-16-be"))
    except UnicodeEncodeError as exc:  # a lone surrogate in a key
        raise CanonicalizationError("object key contains an unpaired surrogate") from exc


def canonicalize(value: object) -> bytes:
    """Return ``value`` as RFC 8785 canonical JSON, UTF-8 encoded.

    Accepts the JSON type set only — ``None``, ``bool``, ``int``, ``float``, ``str``, ``list``/
    ``tuple``, and ``dict`` with string keys. Anything else (``Decimal``, ``datetime``, ``UUID``,
    a Pydantic model) raises :class:`CanonicalizationError` rather than being coerced: the caller
    owns the mapping from a domain object to JSON, and guessing it here is how two callers end up
    hashing the same entry two different ways.

    ``tuple`` is accepted as a JSON array so a frozen preimage structure need not be rebuilt as a
    list purely to be encoded; it canonicalizes identically to the equivalent list, which is
    correct — JSON has one array type.
    """
    return _serialize(value, 0).encode("utf-8")


__all__ = [
    "CANONICAL_ENCODING",
    "CanonicalizationError",
    "canonicalize",
]
