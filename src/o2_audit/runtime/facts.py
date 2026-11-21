"""Fact accumulation view over observations (semantic layer).

Multiple claims for the same (entity, predicate) coexist; conflicts are
resolved by rule layers, never by last-write-wins.
"""
from __future__ import annotations

from o2_audit.domain.models import Entity, EntityKind, Observation, ObservationStatus

PredicateKey = tuple[str, str]


class FactView:
    def __init__(self, entities: list[Entity] | None = None, observations: list[Observation] | None = None) -> None:
        self._entities: dict[str, Entity] = {}
        self._obs: dict[PredicateKey, list[Observation]] = {}
        for e in entities or []:
            self.add_entity(e)
        for o in observations or []:
            self.add_observation(o)

    def add_entity(self, entity: Entity) -> None:
        self._entities.setdefault(entity.entity_id, entity)

    def add_observation(self, obs: Observation) -> None:
        self._obs.setdefault((obs.entity_id, obs.predicate), []).append(obs)

    def entities_of_kind(self, kind: EntityKind) -> list[Entity]:
        return [e for e in self._entities.values() if e.kind == kind]

    def all_entities(self) -> list[Entity]:
        return list(self._entities.values())

    def all_observations(self) -> list[Observation]:
        return [o for claims in self._obs.values() for o in claims]

    def entity(self, entity_id: str) -> Entity | None:
        return self._entities.get(entity_id)

    def claims(self, entity_id: str, predicate: str) -> list[Observation]:
        return list(self._obs.get((entity_id, predicate), []))

    def predicates_of(self, entity_id: str) -> list[str]:
        return sorted({p for (eid, p) in self._obs if eid == entity_id})

    def has_any_claim(self, predicate: str, kinds: tuple[EntityKind, ...]) -> bool:
        for (eid, p), obs_list in self._obs.items():
            if p != predicate:
                continue
            ent = self._entities.get(eid)
            if ent is not None and ent.kind in kinds:
                return True
        return False


def consolidate(claims: list[Observation]) -> tuple[ObservationStatus, object | None, Observation | None]:
    """Collapse coexisting claims of one predicate into a single view.

    - All readable claims agree            -> (READABLE, value, obs)
    - Readable claims disagree             -> (IDENTITY_UNKNOWN, None, first
      conflicting observation) so the source evidence is preserved for the
      rule layer and conflict reporting (never silently dropped to ``None``).
    - Otherwise the most informative non-readable status wins.

    A readable claim whose ``normalized_value`` is ``None`` falls back to its
    ``raw_value`` (the original transcription) when that exists; normalized
    and raw values are never mixed as if they were the same comparison space,
    but a claim must not be invisible only because normalization was skipped.
    """
    def _value(c: Observation) -> object | None:
        return c.normalized_value if c.normalized_value is not None else c.raw_value

    readable = [
        c for c in claims
        if c.status == ObservationStatus.READABLE
        and (c.normalized_value is not None or c.raw_value is not None)
    ]
    if readable:
        values = {repr(_value(c)) for c in readable}
        if len(values) == 1:
            return ObservationStatus.READABLE, _value(readable[0]), readable[0]
        return ObservationStatus.IDENTITY_UNKNOWN, None, readable[0]
    for status in (ObservationStatus.CONFIRMED_MISSING, ObservationStatus.UNREADABLE,
                   ObservationStatus.INCOMPLETE_SCOPE, ObservationStatus.IDENTITY_UNKNOWN):
        for c in claims:
            if c.status == status:
                return status, None, c
    return ObservationStatus.INCOMPLETE_SCOPE, None, claims[0] if claims else None
