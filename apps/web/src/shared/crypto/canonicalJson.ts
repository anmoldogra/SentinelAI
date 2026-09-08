/**
 * RFC 8785 (JCS) canonical JSON — the browser half of the backend's
 * `platform/crypto/canonical.py`.
 *
 * This exists so the console can recompute an evidentiary hash itself rather than trusting the
 * server's claim that a ledger is intact (`api-design.md` §5). Both sides must produce identical
 * bytes for identical values, or an intact ledger is reported as tampered.
 *
 * **It is this short because JavaScript is the language JCS was specified against**, and that is
 * worth stating rather than leaving as a happy accident — the three rules that make a hand-rolled
 * canonicalizer hard in Python are all free here:
 *
 * 1. **Numbers.** JCS §3.2.2.3 mandates ECMAScript `Number::toString`. In JS that is just
 *    `String(n)`. (Python needs an explicit reimplementation to avoid `1.0` and `1e-07`.)
 * 2. **Strings.** JCS §3.2.2.2 defers to `JSON.stringify`'s escaping: minimal escapes, lowercase
 *    `\uXXXX` for control characters, and literal UTF-8 for everything else. (Python's
 *    `json.dumps` defaults to `ensure_ascii=True` and would emit `é`.)
 * 3. **Key order.** JCS §3.2.3 sorts by UTF-16 code unit, which is exactly what JS's default
 *    `Array.prototype.sort()` comparison does on strings. (Python sorts by code point, which
 *    differs above U+FFFF.)
 *
 * So the only real work is walking the structure and refusing what JSON cannot carry. The
 * refusals matter as much as the encoding: silently coercing a value would change a preimage
 * without changing anything visible, which is the failure this whole subsystem exists to prevent.
 */

/** A value RFC 8785 can represent. Anything else is refused rather than coerced. */
export type JsonValue =
  string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export class CanonicalizationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CanonicalizationError";
  }
}

/** Bounded so a pathological structure fails fast instead of exhausting the call stack. */
const MAX_DEPTH = 100;

function serialize(value: JsonValue, depth: number): string {
  if (depth > MAX_DEPTH) {
    throw new CanonicalizationError(`structure nested deeper than ${String(MAX_DEPTH)} levels`);
  }

  if (value === null) {
    return "null";
  }

  const kind = typeof value;

  if (kind === "boolean") {
    return value ? "true" : "false";
  }

  if (kind === "number") {
    const numeric = value as number;
    if (!Number.isFinite(numeric)) {
      // NaN and +/-Infinity have no JSON form. `JSON.stringify` would silently emit `null`,
      // which would make two different values hash identically.
      throw new CanonicalizationError(`${String(numeric)} is not representable in JSON`);
    }
    // ECMAScript Number::toString, which is precisely what JCS requires.
    return String(numeric);
  }

  if (kind === "string") {
    // JSON.stringify implements JCS's escaping rules for strings exactly.
    return JSON.stringify(value);
  }

  if (Array.isArray(value)) {
    return `[${value.map((item) => serialize(item, depth + 1)).join(",")}]`;
  }

  if (kind === "object") {
    const record = value as { [key: string]: JsonValue };
    // Default sort === UTF-16 code unit order === JCS §3.2.3.
    const keys = Object.keys(record).sort();
    const members = keys.map((key) => {
      const entry = record[key];
      if (entry === undefined) {
        // `{a: undefined}` has a key but no JSON value. JSON.stringify drops it; dropping a field
        // from a preimage is exactly the kind of silent difference that breaks verification.
        throw new CanonicalizationError(`key ${JSON.stringify(key)} has an undefined value`);
      }
      return `${JSON.stringify(key)}:${serialize(entry, depth + 1)}`;
    });
    return `{${members.join(",")}}`;
  }

  throw new CanonicalizationError(`${kind} is not a JSON type`);
}

/**
 * Return `value` as an RFC 8785 canonical JSON string.
 *
 * The caller encodes it to UTF-8 (via `TextEncoder`) before hashing; both sides use UTF-8, so the
 * bytes match Python's `canonicalize(...)` output exactly.
 */
export function canonicalJson(value: JsonValue): string {
  return serialize(value, 0);
}
