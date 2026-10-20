"""Skills: instructions and field rules the agent loads by name.

Each skill is a folder skills/{name}/SKILL.md: a front-matter block, then the
instructions the agent follows.

    ---
    name: liquidity
    description: when to use this skill (the router reads this)
    fields: [cash_and_equivalents, total_debt, ...]
    unit: USD millions
    form: 10-Q          # filing whose period end is as_of for not_disclosed rows
    ---
    # Instructions ...

Adding skills/fx/SKILL.md makes an "fx" skill available with no code change.
"""

from dataclasses import dataclass
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


@dataclass
class Skill:
    name: str
    description: str
    fields: list[str]
    unit: str
    form: str
    body: str
    path: Path


def _parse_value(raw: str):
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return [x.strip() for x in raw[1:-1].split(",") if x.strip()]
    return raw


def _parse(path: Path) -> Skill:
    text = path.read_text()
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: missing front matter")
    head, body = text[4:].split("\n---\n", 1)
    meta = {}
    for line in head.splitlines():
        if line.strip():
            key, _, value = line.partition(":")
            meta[key.strip()] = _parse_value(value)
    missing = [k for k in ("name", "description", "fields") if k not in meta]
    if missing:
        raise ValueError(f"{path}: front matter lacks {missing}")
    return Skill(name=meta["name"], description=meta["description"], fields=list(meta["fields"]),
                 unit=meta.get("unit", ""), form=meta.get("form", "10-Q"), body=body.strip(), path=path)


def list_skills() -> list[Skill]:
    return [_parse(p) for p in sorted(SKILLS_DIR.glob("*/SKILL.md"))]


def load_skill(name: str) -> Skill:
    path = SKILLS_DIR / name / "SKILL.md"
    if not path.exists():
        available = ", ".join(s.name for s in list_skills()) or "none"
        raise KeyError(f"no skill named {name!r} (available: {available})")
    return _parse(path)
