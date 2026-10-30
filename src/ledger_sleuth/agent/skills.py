"""Loads skills/<name>/SKILL.md: frontmatter plus body, in a fixed order."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SKILL_NAMES = ("fraud-typologies", "reconciliation-playbook")


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    body: str


def skills_root() -> Path:
    repo_skills = Path(__file__).resolve().parents[3] / "skills"
    if repo_skills.is_dir():
        return repo_skills
    return Path(__file__).resolve().parents[1] / "_skills"  # installed wheel


def parse_skill(text: str) -> Skill:
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must start with frontmatter")
    header, _, body = text[4:].partition("\n---\n")
    fields = {}
    for line in header.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    if not fields.get("name") or not fields.get("description"):
        raise ValueError("SKILL.md frontmatter needs name and description")
    return Skill(fields["name"], fields["description"], body.strip())


def load_skills(root: Path | None = None) -> tuple[Skill, ...]:
    base = root or skills_root()
    return tuple(parse_skill((base / name / "SKILL.md").read_text()) for name in SKILL_NAMES)
