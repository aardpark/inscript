# Prompt Cube: 3D Structural Index for Agent Work

## Status
Spec — not yet implemented. Build when inscript has 50+ sessions with meaningful data density.

## What It Is

A three-dimensional index over all recorded prompts across all sessions. Each prompt occupies a position in a cube defined by three structural dimensions. Querying a region of the cube returns all prompts — across sessions, projects, and agents — that share that structural position.

The cube is inscript's equivalent of tamachi's tami: a single data structure from which everything else (concepts, categories, workflows, decision points, handoffs) falls out as queries over regions.

## The Three Dimensions

### Dimension 1: Category (what kind of work)
Derived from tool-use patterns. Already implemented in `categories.py`.

Values (ordered from exploration to mutation):
```
investigate → search → deep-edit → explore-edit → read-edit → direct → direct-multi → iteration → create → study-create → idle
```

### Dimension 2: Topology (structure of the change)
Derived from the file touches within a prompt.

Values:
```
none        — no files touched (idle prompt)
single-read — read one file
multi-read  — read multiple files
single-edit — edit one existing file
multi-edit  — edit multiple existing files
create-one  — write one new file
create-many — write multiple new files
mixed       — combination of edits and creates
```

Derivation: count of files touched, action types, `is_new` flag in diffs.jsonl.

### Dimension 3: Context (relationship to surrounding work)
Derived from comparing a prompt's file set to adjacent prompts.

Values:
```
origin       — first prompt in a session, or first prompt touching new files
continuation — same files as previous prompt (staying on task)
expansion    — superset of previous prompt's files (growing scope)
narrowing    — subset of previous prompt's files (focusing down)
shift        — different files entirely (context switch)
return       — files that were touched earlier in the session, not recently (coming back)
```

Derivation: set intersection/difference between consecutive prompts' file sets, plus history lookup for `return`.

## Data Structure

Each prompt gets a coordinate:

```python
@dataclass
class PromptCoord:
    category: str       # dimension 1
    topology: str       # dimension 2
    context: str        # dimension 3
    session_id: str
    prompt_idx: int
```

The cube is an inverted index: for each (category, topology, context) triple, store the list of prompts that occupy that position.

```python
# Core structure
cube: dict[tuple[str, str, str], list[PromptCoord]]

# Querying: exact match
cube[("iteration", "multi-edit", "continuation")]
# → all prompts across all sessions where heavy iteration happened
#   on multiple existing files while continuing previous work

# Querying: slice (wildcard one dimension)
[v for k, v in cube.items() if k[0] == "iteration"]
# → all iteration prompts regardless of topology and context

# Querying: region (range on dimensions)
[v for k, v in cube.items() if k[0] in ("iteration", "direct-multi") and k[2] == "shift"]
# → all heavy-edit prompts that were context switches
```

## What Falls Out

### Concepts (existing feature, now a query)
Concepts are currently detected by file co-occurrence clustering. In the cube, a concept is a region that gets revisited across sessions — prompts with similar topology that touch the same file cluster, regardless of category or context.

```python
# "What files keep getting worked on together across sessions?"
# = slice by topology, group by file sets, filter by session count
```

### Categories (existing feature, now a projection)
Categories are dimension 1 projected flat. Already implemented.

### Workflow patterns (existing feature, now a trajectory)
A workflow is a trajectory through the cube — a sequence of coordinates. Recurring workflows are trajectories that repeat across sessions.

```python
# "What sequences of work types happen together?"
# = extract coordinate sequences, find recurring subsequences
```

### Decision points
A decision point is a coordinate position where the *next* coordinate has high variance — many different next-positions have been observed. High branching entropy = decision point.

```python
# "Where do agents face choices?"
# = for each position, measure entropy of next-position distribution
```

### Handoff context
Current position in the cube + recent trajectory + which regions have density in this session. Tells the next agent not just "what happened" but "where in the work-space you are."

### Structural gaps
Regions of the cube that are empty — work patterns that *could* happen but never do. "You never do deep investigation before creating new files" or "you never return to old files after iteration." These are either good practice or blind spots.

### Session shape (enhanced)
Currently session shape is a 1D sequence of categories. In the cube it's a 3D trajectory. Two sessions with the same category sequence but different topology/context patterns are structurally different — one might be exploratory across many files while the other is focused on one file.

## Computation

### When to compute coordinates
Coordinates can be computed lazily or eagerly:
- **Category**: already computed per-prompt by `classify_prompt()`
- **Topology**: computable from `touches.jsonl` per-prompt (count files, check actions)
- **Context**: requires comparing adjacent prompts' file sets (needs session-level pass)

Most efficient: compute all three in a single pass over a session's data when the cube is queried. Cache results in `~/.inscript/sessions/<id>/coords.jsonl`.

### Storage
```jsonl
{"idx": 0, "category": "investigate", "topology": "multi-read", "context": "origin"}
{"idx": 1, "category": "read-edit", "topology": "single-edit", "context": "continuation"}
{"idx": 2, "category": "iteration", "topology": "multi-edit", "context": "expansion"}
```

The global cube index is built in memory from all sessions' coord files. With 1000 sessions × 100 prompts = 100k entries, this is <10MB — no need for a database.

## Relationship to Prior Cube Work

### TamiCube (tatsubot/experiments/suji/tami_cube.py)
TamiCube indexes *code structures* by archetype (accessor, transformer, handler, etc.) using inverted indexes. PromptCube indexes *work patterns* by structural category. Same principle — inverted index over a structural classification — applied to agent behavior instead of code.

### Semantic 3D (tatsubot/mishi/v4/semantic_3d.py)
Semantic 3D decomposes code queries into actions × entities × concepts with weighted scoring. PromptCube decomposes work into category × topology × context. The dimensional decomposition is the same; the domain is different.

### Auto-Generated Cube Snippets (tatsubot/future_patents/)
The patent idea: cube content auto-generated from intent extraction on commits. PromptCube's coordinates are auto-generated from behavioral patterns on prompts. Same principle: the cube populates itself from observed behavior, not hand-authored content.

## When to Build

Prerequisites:
- 50+ sessions with prompt data (enough density for the cube to have populated regions)
- Categories and concepts stable (they become dimensions/queries, not separate systems)
- A concrete use case that the current flat systems can't handle (likely: cross-project workflow comparison, or multi-agent coordination patterns)

Implementation order:
1. Topology classifier (similar to category classifier, applied to file touch patterns)
2. Context classifier (requires adjacent-prompt comparison)
3. Coordinate computation + caching per session
4. Global cube index (in-memory inverted index)
5. Query interface (MCP tool + CLI)
6. Migrate concepts and workflow_patterns to be cube queries instead of standalone

## Design Principle

The cube should make the existing systems *unnecessary as separate code*, not add complexity on top of them. If concepts, categories, and workflows can't be expressed as cube queries, the dimensions are wrong. The right structure makes everything a view.

---
*Spec written 2026-03-20. Builds on: categories.py (dimension 1), concepts.py (becomes a query), TamiCube (structural precedent), Semantic 3D (dimensional decomposition precedent).*
