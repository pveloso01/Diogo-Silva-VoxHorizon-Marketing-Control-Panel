#!/usr/bin/env python3
"""Generate the worker's DB enum mirror from ``lib/supabase/types.gen.ts``.

E0.3 single-source-of-truth: the Postgres enums are the source. ``pnpm
regen:types`` reflects them into ``lib/supabase/types.gen.ts`` (the TS generator).
This script reads the ``Enums:`` block of that generated file and emits
``worker/src/generated/db_enums.py`` so the Python worker stops hand-maintaining
copies of the same status/stage value sets.

The two generators share one upstream (the DB), so the Python enums can never
drift from the TS enums without one of the two drift gates failing.

Usage::

    # regenerate the Python mirror in place
    uv run python scripts/gen_db_enums.py

    # CI / pre-commit drift gate: exit 1 if the committed file is stale
    uv run python scripts/gen_db_enums.py --check

Run both from the ``worker/`` directory (paths are resolved relative to the repo
root, which is the parent of ``worker/``).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Repo layout: this file lives at <repo>/worker/scripts/gen_db_enums.py.
REPO_ROOT = Path(__file__).resolve().parents[2]
TYPES_GEN = REPO_ROOT / "lib" / "supabase" / "types.gen.ts"
OUT_FILE = REPO_ROOT / "worker" / "src" / "generated" / "db_enums.py"

# The subset of enums the Python worker actually consumes, mapped to the public
# constant names emitted in the generated module. Keeping an explicit allow-list
# (rather than emitting all ~40 enums) keeps the generated module small and the
# intent obvious; add a row here when the worker needs another enum.
EXPORTS: dict[str, str] = {
    "pipeline_status_enum": "PIPELINE_STATUSES",
    "pipeline_format_enum": "PIPELINE_FORMATS",
    "creative_stage_enum": "PER_CREATIVE_STAGES",
    "stage_state_enum": "STAGE_STATES",
    "compliance_verdict_enum": "COMPLIANCE_VERDICTS",
    "qa_status_enum": "QA_STATUSES",
    "spec_status_enum": "SPEC_STATUSES",
    "placement_enum": "PLACEMENTS",
    "ratio": "RATIOS",
    "copy_variant_status_enum": "COPY_VARIANT_STATUSES",
    "launch_package_status_enum": "LAUNCH_PACKAGE_STATUSES",
    "ad_entity_kind_enum": "AD_ENTITY_KINDS",
    "ad_entity_state_enum": "AD_ENTITY_STATES",
    "hermes_task_status_enum": "HERMES_TASK_STATUSES",
}


def _extract_enums_block(source: str) -> str:
    """Return the body of the ``Enums: { ... }`` object from ``types.gen.ts``."""
    marker = "    Enums: {\n"
    start = source.find(marker)
    if start == -1:
        raise SystemExit(
            "could not find the `Enums: {` block in types.gen.ts "
            "(did `pnpm regen:types` change the layout?)"
        )
    body_start = start + len(marker)
    # The block closes at the first line that is exactly four-space `}`.
    end = source.find("\n    }\n", body_start)
    if end == -1:
        raise SystemExit("could not find the end of the `Enums` block in types.gen.ts")
    return source[body_start:end]


def _parse_enums(block: str) -> dict[str, list[str]]:
    """Parse ``name: "a" | "b"`` and multi-line union forms into ordered lists.

    The supabase TS generator prints short enums inline and long ones as a
    leading-``|`` multi-line union. We normalise the whole block to a single
    string per enum name, then pull every double-quoted literal in order.
    """
    enums: dict[str, list[str]] = {}
    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is None:
            return
        joined = " ".join(buffer)
        values = re.findall(r'"([^"]+)"', joined)
        enums[current] = values

    # A new enum starts at `      <name>:` (six-space indent, identifier, colon).
    header = re.compile(r"^      ([a-zA-Z_][a-zA-Z0-9_]*):(.*)$")
    for line in block.splitlines():
        m = header.match(line)
        if m:
            flush()
            current = m.group(1)
            buffer = [m.group(2)]
        elif current is not None:
            buffer.append(line)
    flush()
    return enums


def _render(enums: dict[str, list[str]]) -> str:
    """Render the generated Python module text."""
    missing = [name for name in EXPORTS if name not in enums]
    if missing:
        raise SystemExit(
            "types.gen.ts is missing expected enum(s): " + ", ".join(sorted(missing))
        )

    lines: list[str] = [
        '"""Generated DB enum mirror -- DO NOT EDIT BY HAND.',
        "",
        "Source of truth: the Postgres enums, reflected into",
        "``lib/supabase/types.gen.ts`` by ``pnpm regen:types``. Regenerate this",
        "module with::",
        "",
        "    uv run python scripts/gen_db_enums.py",
        "",
        "and verify it in CI with ``--check``. See ``docs/codegen.md``.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Literal",
        "",
    ]

    for enum_name, const_name in EXPORTS.items():
        values = enums[enum_name]
        literal_args = ", ".join(f'"{v}"' for v in values)
        tuple_args = ", ".join(f'"{v}"' for v in values)
        type_name = "".join(part.capitalize() for part in const_name.lower().split("_"))
        lines.append(f"# {enum_name}")
        lines.append(f"{type_name} = Literal[{literal_args}]")
        if len(values) == 1:
            lines.append(f"{const_name}: tuple[{type_name}, ...] = ({tuple_args},)")
        else:
            lines.append(f"{const_name}: tuple[{type_name}, ...] = ({tuple_args})")
        lines.append("")

    all_names = sorted(
        [c for c in EXPORTS.values()]
        + ["".join(p.capitalize() for p in c.lower().split("_")) for c in EXPORTS.values()]
    )
    lines.append("__all__ = [")
    for name in all_names:
        lines.append(f'    "{name}",')
    lines.append("]")
    lines.append("")
    return "\n".join(lines)


def generate() -> str:
    if not TYPES_GEN.exists():
        raise SystemExit(f"types.gen.ts not found at {TYPES_GEN} (run `pnpm regen:types`)")
    source = TYPES_GEN.read_text(encoding="utf-8")
    block = _extract_enums_block(source)
    enums = _parse_enums(block)
    return _render(enums)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the committed db_enums.py differs from a fresh generation",
    )
    args = parser.parse_args(argv)

    rendered = generate()

    if args.check:
        if not OUT_FILE.exists():
            print(
                f"db_enums.py is missing at {OUT_FILE}; run "
                "`uv run python scripts/gen_db_enums.py`",
                file=sys.stderr,
            )
            return 1
        existing = OUT_FILE.read_text(encoding="utf-8")
        if existing != rendered:
            print(
                "db_enums.py is stale -- regenerate with "
                "`uv run python scripts/gen_db_enums.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("db_enums.py is up to date.")
        return 0

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUT_FILE.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
