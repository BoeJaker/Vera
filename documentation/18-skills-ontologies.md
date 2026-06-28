# 18 · Skills & Ontologies

Three related-but-distinct knowledge layers shape how Vera's agents reason. Keeping them straight matters:

| Layer | Module | Group | What it is |
|---|---|---|---|
| **Skills** | `skills/skills.py` | `skills.*` | Named prompt templates that augment LLM/agent behaviour |
| **Domain ontologies** | `skills/skills.py` + `skills/skills_owl.py` | `ontologies.*` | Structured knowledge schemas — *how to process data* |
| **Capability ontology** | `ontologies/cap_ontology.py` | `cap_ontology.*` | A cap×cap relationship matrix — *the system map for the planner* |

Skills and domain ontologies are stored as JSON in memory with optional Redis persistence (`vera:skills:<id>`, `vera:ontologies:<id>`) and reloaded on startup.

---

## 1. Skills

A skill is a reusable prompt fragment: a system-prompt addition, few-shot examples, a chain-of-thought scaffold, or a persona definition. Applying a skill injects its fragment into an LLM/agent call.

| Cap | Purpose |
|---|---|
| `skills.list` / `skills.get` | Browse skills |
| `skills.create` / `skills.update` / `skills.delete` | Skill CRUD |
| `skills.apply` | Resolve a skill into prompt text for a given input |
| `skills.compose` | Combine several skills into one composite fragment |
| `skills.active_context` | The skill context currently in effect (what an agent will actually get) |

---

## 2. Domain ontologies

A domain ontology tells an agent **how to process data** — entity types, relationship rules, context hierarchies, memory-slot definitions, tagging taxonomies. Where a skill shapes *tone and method*, an ontology shapes *structure and meaning*.

| Cap | Purpose |
|---|---|
| `ontologies.list` / `ontologies.get` | Browse ontologies |
| `ontologies.create` / `ontologies.update` / `ontologies.delete` | Ontology CRUD |
| `ontologies.apply` | Apply an ontology's processing rules to data |
| `ontologies.infer` | **LLM**: infer ontology structure from examples |

### OWL interop (`skills_owl.py`)

For interoperability with standard semantic-web tooling, ontologies can be exchanged as OWL/RDF:

| Cap | Purpose |
|---|---|
| `ontologies.list_formats` / `ontologies.schema` | Supported formats & schema |
| `ontologies.export_owl` / `ontologies.import_owl` | Round-trip to OWL |
| `ontologies.owl_context` | OWL view for agent context injection |
| `ontologies.add_class` / `add_property` / `add_restriction` | Edit the OWL model |
| `ontologies.validate` | Validate the ontology |

---

## 3. Capability ontology — the planner's system map

`ontologies/cap_ontology.py` is a different idea entirely. It stores **pairwise relations between capabilities** — a square table of caps × caps where each cell `(X, Y)` describes how cap `X` relates to cap `Y` (feeds, alternative-to, precedes, …). Relations are directed; the reverse edge is separate (optionally inferred for `bidirectional`).

It sits *on top* of the domain ontologies in `skills.py`: those describe domain knowledge; this describes the **capability mesh** — what feeds what, what's an alternative to what — the practical wiring the [DAG planner](./03-dag-engine.md) consults.

Persistence is SQLite (always) + optional Redis cache, with events for live UI updates.

| Cap | Purpose |
|---|---|
| `cap_ontology.list` / `get` / `set` / `bulk_set` / `delete` / `delete_all` | Relation CRUD |
| `cap_ontology.matrix` | The full grid as a sparse object |
| `cap_ontology.neighbours` | Relations touching a cap |
| `cap_ontology.context_for` | A planner-injection snippet for a cap |
| `cap_ontology.descriptions` / `description_get` / `description_set` / `description_delete` | Curated cap descriptions |
| `cap_ontology.stats` / `cap_ontology.jobs` / `cap_ontology.job_status` | Coverage stats & background jobs |
| `cap_ontology.export_to_ontologies` | Promote cap relations into the domain ontology layer |
| `cap_ontology.agent_context` | The relations view assembled for an agent |

### Auto-generation

Relations can be inferred by the LLM from each cap's name, description, and JSON schema, batched to keep prompts small:

| Cap | Purpose |
|---|---|
| `cap_ontology.auto_pair` | Infer one `(from, to)` relation |
| `cap_ontology.auto_group` | Infer all pairs within a group |
| `cap_ontology.auto_grid` | Infer the full grid (long-running) |
| `cap_ontology.auto_group` / `auto_grid` | …with `cap_ontology.suggest` for quick editor hints |

### Planner integration & the "adjacent hidden cap" trick

`cap_ontology.context_for` returns a snippet for a planner system-prompt. When an agent has a restricted `domain_caps` allowlist, this returns relations *between an allowed cap and a hidden cap* — described **by the relation only**. The planner thus learns that an adjacent capability exists (and how it relates) without being able to call it directly: situational awareness without privilege escalation.

---

## 4. UI

| Panel | File | For |
|---|---|---|
| **Skills** | `skills/skills_panel.html` | Skill editor |
| **Ontologies** | `skills/ontologies_panel.html` + `ontologies_owl_panel.js` | Domain ontology + OWL browser |
| **Cap Hub** | `ontologies/cap_ontology_panel.html` | The cap×cap matrix editor, auto-pair/auto-grid runners, coverage stats |

---

## See also

- [Capability Framework](./01-capability-framework.md) — the caps these ontologies describe
- [DAG Engine](./03-dag-engine.md) — the planner that consumes `cap_ontology.context_for`
- [Agents & Chat](./19-agents-chat.md) — agents apply skills and run under `domain_caps` allowlists
- [Memory Graph](./05-memory-graph.md) — domain ontologies shape entity/relationship extraction
