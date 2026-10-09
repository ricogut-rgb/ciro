"""The canvas data model — a forming creation, not a transcript.

A creation is a list of atoms: the smallest meaningful units of content (a decision, a name,
a fact, a draft paragraph). Each carries a significance level — formed (firm, durable substrate),
firming (gaining weight, the active front), or flowing (ephemeral, receding). The canvas
renders THIS, not the conversation.

The said/done seam is the structural floor: a confirmed do is formed; a say is flowing
by default; the classifier promotes creation-relevant says to firming.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Literal


Significance = Literal["formed", "firming", "flowing"]


@dataclass
class Atom:
    content: str
    significance: Significance
    kind: str = "note"
    atom_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source_turn: int = 0
    created_at: float = field(default_factory=time.time)
    parent_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Atom:
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in allowed}
        return cls(**filtered)


class Creation:
    """The forming artifact — atoms arranged by significance, not chronology."""

    def __init__(self, atoms: list[Atom] | None = None):
        self._atoms: list[Atom] = list(atoms or [])

    def add(self, atom: Atom) -> None:
        self._atoms.append(atom)

    def promote(self, atom_id: str, to: Significance) -> bool:
        for a in self._atoms:
            if a.atom_id == atom_id:
                a.significance = to
                return True
        return False

    def atoms_by_significance(self) -> dict[str, list[Atom]]:
        result: dict[str, list[Atom]] = {"formed": [], "firming": [], "flowing": []}
        for a in self._atoms:
            result.setdefault(a.significance, []).append(a)
        return result

    @property
    def atoms(self) -> list[Atom]:
        return list(self._atoms)

    def to_list(self) -> list[dict]:
        return [a.to_dict() for a in self._atoms]

    @classmethod
    def from_list(cls, data: list[dict]) -> Creation:
        atoms = []
        for d in data:
            if isinstance(d, dict):
                try:
                    atoms.append(Atom.from_dict(d))
                except (TypeError, KeyError):
                    continue
        return cls(atoms)
