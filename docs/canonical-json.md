# Canonical JSON — BlackBox canonical JSON v1

Every BlackBox digest hashes one canonical text form: record receipts, observation
evidence receipts, opaque IDs (`identity`), request fingerprints and schema DDL
digests. An independent verifier, for example one checking an exported
[chain head](public-api.md#chain-anchoring), must reproduce these exact bytes.
This document is that specification. It is implemented by `models.canonical` and
frozen by the vectors in `tests/test_canonical.py`.

**This is not RFC 8785 (JCS).** It predates this document and cannot change
without changing every stored digest. The differences are listed below so that a
verifier does not assume JCS.

## Value space

Only these values have a canonical form:

- `null`, `true`, `false`
- integers of any magnitude (JSON numbers without fraction or exponent)
- strings (sequences of Unicode code points; lone surrogates are allowed, see below)
- arrays of canonical values
- objects whose keys are strings and whose values are canonical values

Floating-point numbers, including NaN and infinities, are rejected, as are
non-string object keys. BlackBox's input models accept no floats, so no stored
record contains one. A stored float can only come from tampering, and
verification reports it as an integrity finding.

## Encoding

The canonical text of a value is:

- `null`, `true`, `false`: those literals.
- Integer: the base-10 digits, with `-` for negatives and no leading zeros, `+`,
  fraction or exponent.
- Array: `[`, the elements' canonical texts joined by `,`, then `]`.
- Object: `{`, the members joined by `,`, then `}`. Each member is the key's
  canonical string, `:`, then the value's canonical text. Members are sorted by
  key in ascending **Unicode code point** order.
- String: `"`, each code point escaped as follows, then `"`:
  - `"` → `\"`, `\` → `\\`
  - U+0008 → `\b`, U+000C → `\f`, U+000A → `\n`, U+000D → `\r`, U+0009 → `\t`
  - any other code point below U+0020, U+007F, and every code point above U+007F
    in the Basic Multilingual Plane, including a lone surrogate → `\u` followed by
    4 **lowercase** hex digits
  - code points above U+FFFF → the UTF-16 surrogate pair, each as `\uxxxx`
    (lowercase)
  - every other code point (U+0020–U+007E except `"` and `\`) → itself, so `/`
    is not escaped

There is no whitespace anywhere. The result is pure ASCII, and digests are SHA-256
over its bytes, given as lowercase hex.

Lone surrogates occur in practice. Git paths that are not valid UTF-8 are decoded
with Python's `surrogateescape`, so each undecodable byte becomes U+DC80–U+DCFF.

## Differences from RFC 8785

| Aspect | BlackBox canonical JSON v1 | RFC 8785 |
| --- | --- | --- |
| Non-ASCII in strings | `\u` escapes, lowercase hex | literal UTF-8 |
| U+007F | `\u007f` | literal |
| Key order | Unicode code points | UTF-16 code units (differs for keys above U+FFFF compared with U+E000–U+FFFF) |
| Numbers | integers only; floats rejected | ES6 number serialization |
| Lone surrogates | allowed and escaped | invalid input |

## Examples

| Value | Canonical text |
| --- | --- |
| `{"b": 1, "a": [true, false, null], "c": {"z": "", "y": -7}}` | `{"a":[true,false,null],"b":1,"c":{"y":-7,"z":""}}` |
| `"é"` | `"\u00e9"` |
| `"😀"` | `"\ud83d\ude00"` |
| `{"é": 1, "z": 2, "Z": 3, "😀": 4, "\uffff": 5}` | `{"Z":3,"z":2,"\u00e9":1,"\uffff":5,"\ud83d\ude00":4}` |

The first example's SHA-256 is
`5e54a62526e8157052a583f09d4a7ae62874ce98e8b7b4e7b2796685c80724a6`.

Record material inside a receipt, such as its format domain, fields and chain
link, is described in the [SQLite contract](sqlite-contract.md#record-integrity).
