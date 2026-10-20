"""Service topology: an entry load balancer, the affected service, its upstreams and stores (design step 2)."""

import random
from dataclasses import dataclass, field

from ..models.scenario import ComponentRole
from ..models.snapshot import Component, DependencyEdge
from ..models.vocab import ComponentKind

SERVICE_WORDS = (
    "checkout", "payments", "inventory", "catalog", "orders", "search", "profile", "shipping",
    "pricing", "notify", "auth", "ledger", "reviews", "cart", "billing", "accounts", "media",
)
DATABASE_WORDS = ("orders-db", "users-db", "ledger-db", "catalog-db")
CACHE_WORDS = ("session-cache", "edge-cache", "object-cache")
LB_WORDS = ("edge-lb", "front-lb", "public-lb")
QUEUE_WORDS = ("events-queue", "jobs-queue", "mail-queue")
THIRD_PARTY_WORDS = ("card-gateway", "geo-api", "sms-api", "tax-api")

ROLE_KIND = {
    ComponentRole.ENTRY_LB: ComponentKind.LOAD_BALANCER,
    ComponentRole.AFFECTED: ComponentKind.SERVICE,
    ComponentRole.UPSTREAM: ComponentKind.SERVICE,
    ComponentRole.PEER: ComponentKind.SERVICE,
    ComponentRole.PRIMARY_DB: ComponentKind.DATABASE,
    ComponentRole.CACHE: ComponentKind.CACHE,
    ComponentRole.QUEUE: ComponentKind.QUEUE,
    ComponentRole.THIRD_PARTY: ComponentKind.THIRD_PARTY_API,
}

BASE_ROLES = (
    ComponentRole.ENTRY_LB,
    ComponentRole.AFFECTED,
    ComponentRole.UPSTREAM,
    ComponentRole.UPSTREAM,
    ComponentRole.PRIMARY_DB,
    ComponentRole.CACHE,
    ComponentRole.THIRD_PARTY,
    ComponentRole.PEER,
)
EXTRA_ROLES = (ComponentRole.QUEUE, ComponentRole.PEER, ComponentRole.UPSTREAM, ComponentRole.PEER)


@dataclass
class Topology:
    components: list[Component]
    edges: list[tuple[str, str]]  # (dependent, dependency)
    roles: dict[ComponentRole, list[str]] = field(default_factory=dict)

    def role(self, role: ComponentRole, index: int = 0) -> str:
        return self.roles[role][index]

    def names(self) -> list[str]:
        return [c.name for c in self.components]

    def kind(self, name: str) -> ComponentKind:
        return next(c.kind for c in self.components if c.name == name)

    def upstream_of(self, name: str) -> list[str]:
        return [dependency for dependent, dependency in self.edges if dependent == name]

    def downstream_of(self, name: str) -> list[str]:
        return [dependent for dependent, dependency in self.edges if dependency == name]

    def paths(self, start: str, goal: str) -> list[list[str]]:
        """Every simple dependency path from ``start`` to ``goal`` (following dependent -> dependency)."""
        found: list[list[str]] = []

        def walk(node: str, path: list[str]) -> None:
            if node == goal:
                found.append(path)
                return
            for nxt in self.upstream_of(node):
                if nxt not in path:
                    walk(nxt, [*path, nxt])

        walk(start, [start])
        return found

    def dependency_edges(self, id_for) -> tuple[DependencyEdge, ...]:
        return tuple(
            DependencyEdge(record_id=id_for(f"dep:{a}>{b}"), dependent=a, dependency=b) for a, b in self.edges
        )


def _name(rng: random.Random, words: tuple[str, ...], used: set[str]) -> str:
    for _ in range(100):
        name = f"{rng.choice(words)}-{rng.randrange(16**3):03x}"
        base = name.rsplit("-", 1)[0]
        if base not in {u.rsplit("-", 1)[0] for u in used}:
            used.add(name)
            return name
    name = f"{rng.choice(words)}-{rng.randrange(16**3):03x}"
    used.add(name)
    return name


def build_topology(rng: random.Random, service_count: int) -> Topology:
    roles = list(BASE_ROLES) + [EXTRA_ROLES[i % len(EXTRA_ROLES)] for i in range(service_count - len(BASE_ROLES))]
    words = {
        ComponentKind.SERVICE: SERVICE_WORDS,
        ComponentKind.DATABASE: DATABASE_WORDS,
        ComponentKind.CACHE: CACHE_WORDS,
        ComponentKind.LOAD_BALANCER: LB_WORDS,
        ComponentKind.QUEUE: QUEUE_WORDS,
        ComponentKind.THIRD_PARTY_API: THIRD_PARTY_WORDS,
    }
    used: set[str] = set()
    topology = Topology(components=[], edges=[])
    for role in roles:
        kind = ROLE_KIND[role]
        name = _name(rng, words[kind], used)
        topology.components.append(Component(name=name, kind=kind))
        topology.roles.setdefault(role, []).append(name)

    lb = topology.role(ComponentRole.ENTRY_LB)
    affected = topology.role(ComponentRole.AFFECTED)
    db = topology.role(ComponentRole.PRIMARY_DB)
    cache = topology.role(ComponentRole.CACHE)
    third = topology.role(ComponentRole.THIRD_PARTY)
    upstreams = topology.roles[ComponentRole.UPSTREAM]
    peers = topology.roles.get(ComponentRole.PEER, [])
    queues = topology.roles.get(ComponentRole.QUEUE, [])

    edges = [(lb, affected), *((lb, p) for p in peers)]
    edges += [(affected, u) for u in upstreams] + [(affected, db), (affected, cache)]
    edges.append((upstreams[0], third))
    edges.append((upstreams[1], queues[0] if queues else db))
    for extra in upstreams[2:]:
        edges.append((extra, db))
    for peer in peers:
        edges.append((peer, upstreams[1]))
    for queue in queues:
        edges.append((queue, db))
    for queue in queues[1:]:
        edges.append((upstreams[-1], queue))
    topology.edges = list(dict.fromkeys(edges))
    return topology
