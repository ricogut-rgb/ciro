"""The constitution, as code. Model-independent.

Confirm is the only admit. No model output can fire an action; the model may only PROPOSE,
and only a human confirm admits. The same gate holds whether a frontier model or a hostile
stub runs above it — because the guarantee is structural (a typed registry + a confirm step),
not a scanned prompt.

Only action types in ACTION_REGISTRY can ever fire, and only through propose() → confirm().
There is deliberately NO method a model can reach that fires an action.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# The only actions that can ever fire. Unknown types are refused (never-inflate: the machine
# cannot act outside what it was given). Each names its reversibility and cost honestly.
ACTION_REGISTRY: dict[str, dict] = {
    "write_note": {
        "reversible": True,
        "costs_money": False,
        "params": "text",
        "describe": lambda p: f'write a note: "{p.get("text", "")}"',
    },
    "spend": {
        "reversible": False,
        "costs_money": True,
        "params": "amount, what",
        "describe": lambda p: (
            f'spend ${p.get("amount", "?")} on {p.get("what", "?")} '
            f"— irreversible, real money"
        ),
    },
}


@dataclass
class Gate:
    _pending: dict[str, tuple[str, dict]] = field(default_factory=dict)
    _fired: list[tuple[str, dict]] = field(default_factory=list)
    _n: int = 0

    def propose(self, action_type: str, params: dict) -> dict:
        """The only entry a model's intent can reach. Returns a projection to confirm — or a refusal."""
        if action_type not in ACTION_REGISTRY:
            return {"ok": False, "kind": "unregistered", "action_type": action_type}
        self._n += 1
        move_id = f"m{self._n}"
        self._pending[move_id] = (action_type, dict(params))
        spec = ACTION_REGISTRY[action_type]
        projection = spec["describe"](params)
        if spec["costs_money"] or not spec["reversible"]:
            projection += "  [nothing happens until you confirm; this cannot be undone]"
        else:
            projection += "  [nothing happens until you confirm]"
        return {"ok": True, "move_id": move_id, "projection": projection}

    def confirm(self, move_id: str) -> dict:
        """Called by the HUMAN SEAT only — never by a model. Admits one proposed action."""
        if move_id not in self._pending:
            return {"ok": False, "kind": "no_such_pending"}
        action_type, params = self._pending.pop(move_id)
        self._fired.append((action_type, params))
        return {"ok": True, "fired": action_type, "params": params}

    @property
    def fired(self) -> list[tuple[str, dict]]:
        return list(self._fired)

    @staticmethod
    def offer() -> str:
        """The only actions on offer. The model is told these and nothing else (injected-handles
        discipline). It may propose from this list; anything outside it is refused at propose()."""
        return "; ".join(f'{a} (params: {s["params"]})' for a, s in ACTION_REGISTRY.items())
