"""announce.py — the CEP-6 announcement emitter (Python port of the shared kit).

WHY THIS FILE EXISTS
====================

The nosms CVM server is Python; the tag contract is owned by
``cvm-services/cvm-service-kit``, which at the vendored commit
``7bf4be6`` (branch ``pr/s2a-announce-emitter``) is TypeScript/Deno and has no
Python bindings. The card's instruction is explicit: *do not fork the kit — port
it and PROVE parity*.

So this is a faithful port of ``src/announce.ts`` + ``src/vocab.ts`` +
``src/validate.ts``, and ``tests/test_announce_parity.py`` diffs its output
against the golden fixture the **Deno kit itself** produced
(``tests/fixtures/nosms-11316.tags.json``). If the kit changes, the fixture is
regenerated in ``contextvm-services`` and this module must be updated to match —
in one reviewed commit, in one direction.

THE RULES THIS ENCODES (verbatim from the spec, ``docs/spec/service-inputs.md``)
==============================================================================

* the tier tag is ``["t","cvm:tier:<tier>"]``, exactly ONE per announcement;
* it MUST equal the recomputed MAX of the declared ``cvm:req:*``/``cvm:opt:*``
  fields (ranks none 0 < financial 1 < contact 2 < fulfilment 3 < legal 4 <
  sensitive 5);
* "the field list is the truth": a reader that finds a disagreement uses the
  RECOMPUTED value and surfaces the mismatch;
* absent is not ``none``: no ``cvm:req:*``/``cvm:opt:*`` tag at all is an UNKNOWN
  appetite (``recompute_tier`` returns ``None``), never ``none``;
* unknown field names fail loud: surfaced, treated at the most restrictive rank,
  never counted as ``cvm:req:none``;
* the caller CANNOT supply the tier — the emitter computes it, so a lying
  aggregate is not expressible.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --- ladder -----------------------------------------------------------------

TIER_LADDER: tuple[str, ...] = ("none", "financial", "contact", "fulfilment", "legal", "sensitive")

#: Upper-bound ordering used for FILTERING only. Not a juridical claim.
TIER_RANKS: dict[str, int] = {
    "none": 0,
    "financial": 1,
    "contact": 2,
    "fulfilment": 3,
    "legal": 4,
    "sensitive": 5,
}

#: Namespaced values carried on the single-letter ``t`` tag (ADR-0001 D2/D3).
CLASS_PREFIX = "cvm:service:"
REQ_PREFIX = "cvm:req:"
OPT_PREFIX = "cvm:opt:"
TIER_PREFIX = "cvm:tier:"

#: ``cvm:req:none`` — the tag form of "tiers none/financial only".
NONE_SENTINEL = "cvm:req:none"

#: CEP-6 server announcement (replaceable).
ANNOUNCEMENT_KIND = 11316

#: Server-side prefilter values for a UI shorthand (vocab.filter_shorthand).
TIER_SHORTHAND: dict[str, tuple[str, ...]] = {
    "no_personal_data": ("none", "financial"),
    "contact_only": ("none", "financial", "contact"),
}

DEFAULT_VOCAB_PATH = Path(__file__).resolve().parent.parent / "vocab" / "service-inputs.json"

_KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FIELD = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)*$")
_GEOHASH = re.compile(r"^[0-9b-hjkmnp-z]+$")


class AnnounceError(ValueError):
    """A caller error: the input cannot be emitted as a conforming announcement."""


# --- register ---------------------------------------------------------------


def load_vocab(path: str | Path | None = None) -> dict[str, Any]:
    """Load the vendored register. Structural parse only; content check is separate."""
    p = Path(path) if path is not None else DEFAULT_VOCAB_PATH
    raw = json.loads(Path(p).read_text())
    if not isinstance(raw, dict):
        raise AnnounceError("vocab: not an object")
    if not isinstance(raw.get("fields"), dict):
        raise AnnounceError("vocab: missing 'fields'")
    if not isinstance(raw.get("tiers"), dict):
        raise AnnounceError("vocab: missing 'tiers'")
    return raw


def vocab_errors(vocab: dict[str, Any]) -> list[str]:
    """Content checks that make the tier rules meaningful (mirror of vocabErrors)."""
    errors: list[str] = []
    for tier in TIER_LADDER:
        if tier not in vocab.get("tiers", {}):
            errors.append(f"tiers is missing '{tier}'")
    ranks = (vocab.get("tier_tag") or {}).get("ranks")
    if not ranks:
        errors.append("tier_tag.ranks is missing (the ladder must be published, not inferred)")
    else:
        for tier in TIER_LADDER:
            if ranks.get(tier) != TIER_RANKS[tier]:
                errors.append(
                    f"tier_tag.ranks['{tier}'] = {ranks.get(tier)}, want {TIER_RANKS[tier]} (the ladder is fixed)"
                )
    for name, f in (vocab.get("fields") or {}).items():
        if not isinstance(f, dict) or not isinstance(f.get("tier"), str):
            errors.append(f"field '{name}' has no tier")
            continue
        if f["tier"] not in TIER_LADDER:
            errors.append(f"field '{name}' has unknown tier '{f['tier']}'")
    return errors


def is_tier(v: str) -> bool:
    return v in TIER_LADDER


def rank_of(tier: str) -> int | None:
    return TIER_RANKS.get(tier)


def higher_tier(a: str, b: str) -> str:
    return a if TIER_RANKS[a] >= TIER_RANKS[b] else b


def field_tier(name: str, vocab: dict[str, Any]) -> str | None:
    t = (vocab.get("fields") or {}).get(name, {}).get("tier")
    return t if isinstance(t, str) and is_tier(t) else None


def tag_values(tags: list[list[str]], name: str) -> list[str]:
    return [t[1] for t in tags if isinstance(t, list) and len(t) >= 2 and t[0] == name and isinstance(t[1], str)]


def uniq_sorted(xs: list[str]) -> list[str]:
    return sorted(set(xs))


def uniq_strings(xs: list[str]) -> list[str]:
    """Trim, drop empties, de-duplicate — CASE PRESERVED (a URL's path is not lowercased)."""
    out: list[str] = []
    for x in xs:
        v = str(x).strip()
        if v and v not in out:
            out.append(v)
    return out


@dataclass
class DeclaredInputs:
    required: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    none_sentinel: bool = False
    unclassified: bool = False


def declared_inputs(tags: list[list[str]]) -> DeclaredInputs:
    t = tag_values(tags, "t")
    req_all = [v[len(REQ_PREFIX):] for v in t if v.startswith(REQ_PREFIX)]
    opt_all = [v[len(OPT_PREFIX):] for v in t if v.startswith(OPT_PREFIX)]
    return DeclaredInputs(
        required=uniq_sorted([f for f in req_all if f != "none"]),
        optional=uniq_sorted([f for f in opt_all if f != "none"]),
        none_sentinel="none" in req_all,
        unclassified=(len(req_all) == 0 and len(opt_all) == 0),
    )


@dataclass
class TierRecompute:
    tier: str | None  # None = unknown appetite (unclassified), NOT "none"
    unknown: list[str]
    basis: list[str]


def recompute_tier(d: DeclaredInputs, vocab: dict[str, Any]) -> TierRecompute:
    if d.unclassified:
        return TierRecompute(tier=None, unknown=[], basis=[])
    basis = uniq_sorted([*d.required, *d.optional])
    unknown = [f for f in basis if f not in (vocab.get("fields") or {})]
    if not basis:
        # only the sentinel: nothing user-supplied -> the bottom of the ladder
        return TierRecompute(tier="none", unknown=[], basis=[])
    best = "none"
    for f in basis:
        declared = field_tier(f, vocab)
        tier = declared if declared is not None else "sensitive"  # unknown fails loud
        best = higher_tier(best, tier)
    return TierRecompute(tier=best, unknown=unknown, basis=basis)


def recompute_tier_from_tags(tags: list[list[str]], vocab: dict[str, Any]) -> TierRecompute:
    return recompute_tier(declared_inputs(tags), vocab)


def tier_prefilter(shorthand: str) -> list[str]:
    tiers = TIER_SHORTHAND.get(shorthand)
    if tiers is None:
        raise AnnounceError(f"unknown tier shorthand '{shorthand}'")
    return [TIER_PREFIX + t for t in tiers]


# --- validation (reader side) ----------------------------------------------


@dataclass
class Assessment:
    declared_tiers: list[str]
    recomputed: str | None
    effective: str | None
    tier_mismatch: bool
    declared: DeclaredInputs
    unknown_fields: list[str]
    violations: list[str]
    warnings: list[str]


def assess_announcement_tags(tags: list[list[str]], vocab: dict[str, Any]) -> Assessment:
    inputs = declared_inputs(tags)
    rec = recompute_tier(inputs, vocab)
    declared_tiers = sorted({v[len(TIER_PREFIX):] for v in tag_values(tags, "t") if v.startswith(TIER_PREFIX)})

    violations: list[str] = []
    warnings: list[str] = []

    for t in declared_tiers:
        if not is_tier(t):
            violations.append(f"cvm:tier:{t} is not a ladder value")
    known = [t for t in declared_tiers if is_tier(t)]

    if inputs.unclassified:
        if declared_tiers:
            violations.append(
                "a cvm:tier tag is published but no cvm:req:*/cvm:opt:* tag exists (absent is not 'none')"
            )
    elif len(known) != 1:
        violations.append(f"expected exactly one cvm:tier tag, found {len(known)}")

    tier_mismatch = (
        not inputs.unclassified and len(known) == 1 and rec.tier is not None and known[0] != rec.tier
    )
    if tier_mismatch:
        violations.append(
            f"tier tag says '{known[0]}' but the declared fields recompute to '{rec.tier}' "
            "(the field list is the truth; use the recomputed value)"
        )

    if rec.unknown:
        violations.append(
            f"unknown requirement field(s): {', '.join(rec.unknown)} "
            "(unknown fails loud and is never counted as cvm:req:none)"
        )

    both = [f for f in inputs.required if f in inputs.optional]
    if both:
        violations.append(f"field(s) declared both required and optional: {', '.join(both)}")

    effective = rec.tier
    if not inputs.unclassified and effective is not None:
        no_personal_data = TIER_RANKS[effective] <= TIER_RANKS["financial"]
        if no_personal_data and not inputs.none_sentinel:
            warnings.append(
                "tier is none/financial but no cvm:req:none sentinel is published "
                "(the shorthand reads cvm:req:none as 'none/financial only')"
            )
        if not no_personal_data and inputs.none_sentinel:
            warnings.append(f"cvm:req:none sentinel published although the recomputed tier is '{effective}'")

    return Assessment(
        declared_tiers=declared_tiers,
        recomputed=rec.tier,
        effective=effective,
        tier_mismatch=tier_mismatch,
        declared=inputs,
        unknown_fields=rec.unknown,
        violations=violations,
        warnings=warnings,
    )


def assert_announcement_tags(tags: list[list[str]], vocab: dict[str, Any]) -> Assessment:
    """Raise on a non-conforming announcement; return the assessment otherwise."""
    a = assess_announcement_tags(tags, vocab)
    if a.violations:
        raise AnnounceError("announcement violates the tag contract:\n - " + "\n - ".join(a.violations))
    return a


# --- emitter (build side) ---------------------------------------------------


@dataclass
class ToolCap:
    amount: int
    unit: str = "sats"


@dataclass
class AnnounceInput:
    service_class: str
    d: str
    geohashes: list[str] = field(default_factory=list)
    required: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    tools: dict[str, ToolCap] = field(default_factory=dict)
    registries: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    human_tags: list[str] = field(default_factory=list)
    content: Any = None
    allow_unknown_fields: bool = False


@dataclass
class EmittedAnnouncement:
    kind: int
    tags: list[list[str]]
    content: str
    tier: str
    warnings: list[str]


def emit_announcement_tags(input: AnnounceInput, vocab: dict[str, Any]) -> tuple[list[list[str]], list[str], str]:
    """Port of ``emitAnnouncementTags``. Returns ``(tags, warnings, tier)``."""
    warnings: list[str] = []

    # --- class + slug -------------------------------------------------------
    service_class = (input.service_class or "").strip()
    if not _KEBAB.match(service_class) or ":" in service_class:
        raise AnnounceError(
            f"serviceClass '{input.service_class}' is not a short lowercase kebab token (P2)"
        )
    d = (input.d or "").strip()
    if not d or re.search(r"\s", d):
        raise AnnounceError(f"d slug '{input.d}' is empty or contains whitespace (P1)")

    # --- declared fields ----------------------------------------------------
    required = _clean_fields(input.required, "required", warnings)
    optional = _clean_fields(input.optional, "optional", warnings)
    both = [f for f in required if f in optional]
    if both:
        raise AnnounceError(f"field(s) declared both required and optional: {', '.join(both)}")
    for f in [*required, *optional]:
        if f not in (vocab.get("fields") or {}):
            if not input.allow_unknown_fields:
                raise AnnounceError(
                    f"unknown requirement field '{f}': not in the register "
                    "(vocab/service-inputs.json). Add it to the register, or pass "
                    "allowUnknownFields to emit it at the most restrictive tier — never as 'none'."
                )
            warnings.append(
                f"unknown requirement field '{f}': emitted at the most restrictive tier and flagged "
                "(rule 3: unknown fails loud, never cvm:req:none)"
            )

    # --- the tier is COMPUTED, never supplied -------------------------------
    declared = DeclaredInputs(
        required=required,
        optional=optional,
        # sentinel presence does not affect the recompute (it contributes no field)
        none_sentinel=(len(required) == 0),
        unclassified=False,
    )
    rec = recompute_tier(declared, vocab)
    tier = rec.tier if rec.tier is not None else "none"

    # --- geohash precisions -------------------------------------------------
    # The guard must see the TAGS about to be emitted, not the raw input: `#g` is
    # exact match, so duplicates collapsing to one `g` tag are the same silent
    # discovery failure as publishing one precision (P2/D3). Dedupe first.
    geohashes = uniq_strings(input.geohashes)
    if len(geohashes) == 1:
        raise AnnounceError(
            "geohashes: exactly one distinct precision — #g is exact match, publish two or more "
            "distinct precisions of the same point (P2), or none if the service has no fixed location"
        )
    if len(geohashes) > 1:
        longest = max(geohashes, key=len)
        for g in geohashes:
            if not g or not _GEOHASH.match(g):
                raise AnnounceError(f"geohash '{g}' is not a geohash")
            if not longest.startswith(g):
                raise AnnounceError(
                    f"geohash '{g}' is not a prefix of '{longest}': the precisions must describe ONE point"
                )

    # --- assemble, deterministic order --------------------------------------
    tags: list[list[str]] = []
    tags.append(["d", d])
    tags.append(["t", CLASS_PREFIX + service_class])
    for w in uniq_strings(input.human_tags):
        if ":" in w:
            raise AnnounceError(f"human t word '{w}' is namespaced; use the class field")
        tags.append(["t", w])
    for f in required:
        tags.append(["t", REQ_PREFIX + f])
    for f in optional:
        tags.append(["t", OPT_PREFIX + f])
    # Sentinel rule: emit it exactly when the recomputed tier is none or financial;
    # never alongside a higher tier.
    if TIER_RANKS[tier] <= TIER_RANKS["financial"]:
        tags.append(["t", NONE_SENTINEL])
    tags.append(["t", TIER_PREFIX + tier])
    for g in sorted(geohashes, key=lambda x: (len(x), x)):
        tags.append(["g", g])
    for tool in sorted(input.tools):
        cap = input.tools[tool]
        if not tool.strip():
            raise AnnounceError("cap: tool name is empty")
        amount = cap.amount
        if not isinstance(amount, (int, float)) or amount != amount or amount in (float("inf"), float("-inf")) or amount < 0:
            raise AnnounceError(f"cap: tool '{tool}' has a non-finite or negative amount")
        tags.append(["cap", f"tool:{tool}", str(int(amount) if float(amount).is_integer() else amount), cap.unit or "sats"])
    for a in input.registries:
        tags.append(["a", a])
    for r in uniq_strings(input.urls):
        tags.append(["r", r])

    # --- self-check: the emitter can never emit a non-conforming set --------
    assessment = assess_announcement_tags(tags, vocab)
    violations = [
        v
        for v in assessment.violations
        if not (input.allow_unknown_fields and v.startswith("unknown requirement field"))
    ]
    if violations:
        raise AnnounceError(
            "emitter produced a non-conforming announcement (this is a port bug):\n - "
            + "\n - ".join(violations)
        )
    if assessment.tier_mismatch or assessment.effective != tier:
        raise AnnounceError("emitter produced a tier that does not recompute (this is a port bug)")
    warnings.extend(assessment.warnings)
    return tags, warnings, tier


def emit_announcement(input: AnnounceInput, vocab: dict[str, Any]) -> EmittedAnnouncement:
    tags, warnings, tier = emit_announcement_tags(input, vocab)
    content = "" if input.content is None else json.dumps(input.content)
    return EmittedAnnouncement(kind=ANNOUNCEMENT_KIND, tags=tags, content=content, tier=tier, warnings=warnings)


def _clean_fields(fields: list[str], side: str, warnings: list[str]) -> list[str]:
    out: list[str] = []
    for raw in fields:
        f = str(raw).strip()
        if f == "none":
            # the sentinel is not a field; the emitter derives it from the tier
            warnings.append(f"'cvm:req:none' passed as a {side} field: ignored (the emitter derives the sentinel)")
            continue
        if not _FIELD.match(f):
            raise AnnounceError(f"{side} field '{raw}' is not domain.field lowercase snake_case")
        if f not in out:
            out.append(f)
    return sorted(out)
