"""
SkillLoader — Pure Python loader for vendored agent-skills (Addy Osmani).

Design goals (Milestone 1):
- Discover skills strictly from the filesystem (never hardcode names).
- Parse YAML frontmatter + Markdown body.
- Provide flexible section extraction (real skills use "The X Process", not always literal "## Process").
- 100% offline testable (no network, no subprocess).
- Graceful errors on unknown skills.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml


# Resolve relative to this file: src/crewai_headless_flow/skills/loader.py
# parents[0]=skills, [1]=crewai_headless_flow, [2]=src, [3]=project root
VENDOR_ROOT = Path(__file__).resolve().parents[3] / "vendor" / "agent-skills" / "skills"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    body: str  # Full Markdown body after frontmatter


class SkillLoader:
    """Loads and serves skills from the vendored agent-skills directory."""

    def __init__(self, vendor_root: Optional[Path] = None) -> None:
        self._vendor_root = vendor_root or VENDOR_ROOT
        self._cache: Dict[str, Skill] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return

        if not self._vendor_root.exists():
            raise RuntimeError(
                f"Vendor skills directory not found: {self._vendor_root}. "
                "Run the vendoring script or ensure vendor/agent-skills/skills/ exists."
            )

        for skill_dir in sorted(self._vendor_root.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue

            skill = self._parse_skill_file(skill_file)
            self._cache[skill.name] = skill

        self._loaded = True

    def _parse_skill_file(self, path: Path) -> Skill:
        text = path.read_text(encoding="utf-8")

        # Split YAML frontmatter (--- ... ---)
        if text.startswith("---"):
            _, front, body = text.split("---", 2)
            frontmatter = yaml.safe_load(front) or {}
        else:
            frontmatter = {}
            body = text

        name = frontmatter.get("name") or path.parent.name
        description = frontmatter.get("description", "")

        return Skill(
            name=str(name),
            description=str(description),
            path=path,
            body=body.strip(),
        )

    def list_skills(self) -> List[str]:
        """Return sorted list of discovered skill names (derived from filesystem)."""
        self._ensure_loaded()
        return sorted(self._cache.keys())

    def get_skill(self, name: str) -> Skill:
        """Return full Skill object. Raises KeyError for unknown skill."""
        self._ensure_loaded()
        if name not in self._cache:
            available = ", ".join(self.list_skills())
            raise KeyError(f"Unknown skill '{name}'. Available: {available}")
        return self._cache[name]

    def get_section(self, name: str, section_hint: str = "Process") -> str:
        """
        Extract the core procedural guidance for a skill.

        Real skills use varied headings:
          - "The Planning Process"
          - "The Gated Workflow"
          - "The Increment Cycle"
          - etc.

        Strategy:
        1. Try to find a heading containing the hint (case-insensitive).
        2. If not found, return a sensible "core guidance" slice:
           - Everything after the first "## " heading block that looks procedural
             (Overview + When to Use + first major process section).
        3. As last resort, return the full body (minus frontmatter).
        """
        skill = self.get_skill(name)
        body = skill.body

        # Try direct hint match (e.g. "Process", "Gated Workflow", "Increment Cycle")
        pattern = re.compile(
            rf"^##\s+.*{re.escape(section_hint)}.*$", re.IGNORECASE | re.MULTILINE
        )
        match = pattern.search(body)
        if match:
            start = match.start()
            # Find next top-level heading or end of file
            next_heading = re.search(r"^##\s+", body[start + 1 :], re.MULTILINE)
            if next_heading:
                return body[start : start + 1 + next_heading.start()].strip()
            return body[start:].strip()

        # Fallback: return Overview + When to Use + first major process section
        # This gives the agent the "why + when + how" without the entire file.
        sections = re.split(r"^##\s+", body, flags=re.MULTILINE)
        useful = []
        for sec in sections:
            sec = sec.strip()
            if not sec:
                continue
            lower = sec.lower()
            if any(
                k in lower
                for k in [
                    "overview",
                    "when to use",
                    "process",
                    "cycle",
                    "workflow",
                    "gated",
                ]
            ):
                useful.append("## " + sec)
            if len(useful) >= 3:
                break

        if useful:
            return "\n\n".join(useful).strip()

        # Last resort
        return body.strip()

    def get_core_guidance(self, name: str) -> str:
        """Convenience alias used by wiring code."""
        return self.get_section(name, "Process")

    def print_discovered(self) -> None:
        """Pretty print for startup / debugging."""
        skills = self.list_skills()
        print(f"Discovered {len(skills)} skills from {self._vendor_root}:")
        for name in skills:
            skill = self._cache[name]
            print(
                f"  - {name}: {skill.description[:80]}{'...' if len(skill.description) > 80 else ''}"
            )


# Singleton for convenience in early milestones
_default_loader: Optional[SkillLoader] = None


def get_default_loader() -> SkillLoader:
    global _default_loader
    if _default_loader is None:
        _default_loader = SkillLoader()
    return _default_loader
