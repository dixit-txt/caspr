# Casper graph: jugaad → native LangGraph

> Engineering research note. Generated from a read of `caspr-api/src/core/model.py`
> (4520 lines) and the domain subgraphs. Line numbers as of 2026-09-02.
> Verify each API against the installed `langgraph 1.1.10` docs before implementing.

The chat orchestration in `caspr-api/src/core/model.py` runs on LangGraph but
reimplements a lot of what the framework already does — persistence,
human-in-the-loop, tool sequencing, ordering constraints, cross-node state
passing, provider fallback. This is an inventory of each workaround and the
native capability that replaces it.

**Installed today:** `langgraph 1.1.10` · `langchain-core 1.3` · `langchain-anthropic 1.0`.
Every capability below ships in that version. `requirements.txt` still pins
`langgraph>=0.2,<0.3` — **step one is closing that gap.**

---

## The root cause: a stateless graph

Almost every workaround traces back to one decision: the graph is compiled with
**no checkpointer** (`graph_builder.compile()`, model.py:4315). It holds no memory
between messages, so:

- A fresh `Casper` object is built for every user turn, and the full conversation
  is rebuilt by hand from `user_previous_messages` on each request.
- State that *should* live in graph state instead lives on the object
  (`self.domain_name`, `self.retrieve_config`, `self._current_turn_messages`,
  `self._latest_info_context`) or gets smuggled through the message list as
  specially-marked `ToolMessage`s.
- The graph can't pause and resume, so "ask the user a question" is faked with
  prompt injection instead of a real interrupt.

> **The single highest-leverage change** is adding a checkpointer keyed by
> `chat_id`. It directly removes the manual history reconstruction in
> `get_processing_state()`, and it's the prerequisite that makes native
> `interrupt()` and durable custom state possible.

---

## Inventory at a glance

| #  | Workaround in model.py | Native LangGraph capability | Lift |
|----|------------------------|-----------------------------|------|
| S1 | Manual history rebuild from `user_previous_messages` — orphan-tool stripping, incomplete tool-pair removal (`get_processing_state`, ~70 lines) | `compile(checkpointer=…)` + `thread_id=chat_id` | Medium |
| S2 | Cross-node state on the instance: `self.domain_name`, `self.retrieve_config`, `self._latest_info_context` | Custom `State` schema + tools returning `Command(update=…)` | Medium |
| S3 | `self._current_turn_messages` stashed so tools can read the conversation | `Annotated[dict, InjectedState]` tool arg | Small |
| H1 | `ask_user`: emits an event, returns "wait for the user's next message", plus a system-prompt patch forcing the model not to call tools, plus gate logic collapsing it to one call | `interrupt()` + `Command(resume=…)` | Medium |
| T1 | `_gate_node` rewrites the `AIMessage` in place (reused id) to collapse parallel tool calls | `bind_tools(tools, parallel_tool_calls=False)` | Small |
| T2 | `_sequential_tools_node` — a hand-rolled `ToolNode` reimplementation (dispatch, error stubs, id-pairing, per-turn caps) | Prebuilt `ToolNode` once T1 lands; caps in a pre-model node | Medium |
| T3 | Per-turn tool-call counting by walking messages backwards (`_count_turn_tool_calls`, duplicated in `report_or_respond`) | Counter in custom state, reset on human input | Small |
| F1 | Layout-first ordering guard: `_LAYOUT_GUARD_MARKER` nudge ToolMessages, 2-try bound, "already answered" routing check, system-prompt patches in two places | Forced `tool_choice` + a `layout_proposed` state flag on a conditional edge | Medium |
| F2 | Background layout refresh via `asyncio.create_task`, `_await_layout_refresh` (150 s timeout), and `_patched_layout_tool_message` rewriting a past ToolMessage | A real graph node on a parallel branch (fan-out / deferred node) | Large |
| P1 | `run_primary_research` / `run_due_diligence` manually `astream` the subgraph and re-emit every custom event | Add the compiled subgraph directly as a node; `stream(subgraphs=True)` | Medium |
| R1 | Provider fallback as nested `try/except` (Anthropic→OpenAI→Gemini) in `report_or_respond` and `generate_chat_title` | `llm.with_fallbacks([...])` · `with_structured_output` · node `RetryPolicy` | Small |

---

## State & persistence

The graph should own the thread. Everything the turn produces belongs in
checkpointed state, not on the Python object.

### S1 — Manual history reconstruction

`get_processing_state()` · model.py:4443–4515

**The jugaad.** Each request rebuilds `chat_messages` from
`user_previous_messages`: walk the list, drop `ai`/`tool` pairs whose turn never
completed, strip orphaned leading `tool` messages, strip a stale `system`
message, re-prepend the current one. ~70 lines of defensive list surgery that
exists only because nothing persists between calls.

**Native LangGraph.** Compile with a **checkpointer** and pass `thread_id`.
LangGraph loads prior state, appends via the `messages` reducer, and persists
atomically — a half-finished turn is rolled back to the last checkpoint instead
of hand-repaired.

```python
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

# one saver for the process, reused across chats
graph = builder.compile(checkpointer=saver)

await graph.ainvoke(
    {"messages": [HumanMessage(query)]},
    config={"configurable": {"thread_id": chat_id}},
)
```

You already store messages in your own DB — keep that as the system of record and
use `AsyncPostgresSaver` against the same instance, or write a thin custom
`BaseCheckpointSaver` over the existing table. Run the Postgres saver's `setup()`
migration once.

### S2 — Cross-node state carried on `self`

`retrieve()` sets `self.domain_name` / `self.retrieve_config` → `_route_by_domain` reads them

**The jugaad.** The `retrieve` tool writes `self.domain_name`,
`self.retrieve_config`, `self._latest_info_context`. Later nodes
(`_route_by_domain`, `run_primary_research`) read them off the instance. The
graph's data flow is invisible — it runs through object attributes, so it can't
be checkpointed, replayed, or run concurrently on one `Casper`.

**Native LangGraph.** Extend the state schema, and let the tool **return a
`Command`** that updates state and emits its `ToolMessage` in one shot. Routing
then reads `state["domain_name"]`.

```python
class CasperState(MessagesState):
    domain_name: str
    retrieve_config: dict
    layout_proposed: bool
    turn_tool_calls: dict

async def retrieve(..., tool_call_id: Annotated[str, InjectedToolCallId]):
    return Command(update={
        "domain_name": domain_name,
        "retrieve_config": cfg,
        "messages": [ToolMessage(content, tool_call_id=tool_call_id)],
    })
```

### S3 — `self._current_turn_messages` stash

`report_or_respond:2455` · read by `propose_report_layout` / `update_proposed_report_layout`

**The jugaad.** `report_or_respond` copies `state["messages"]` onto the instance
because "tools are invoked without state." Tools that need the conversation (the
layout refresh) then read the stash.

**Native LangGraph.** Tools *can* receive state. Annotate a parameter with
`InjectedState` — LangGraph fills it and hides it from the model's schema.

```python
from langgraph.prebuilt import InjectedState

async def propose_report_layout(
    report_layout: str,
    state: Annotated[dict, InjectedState],
):
    conversation = state["messages"]
```

---

## Human-in-the-loop

### H1 — `ask_user` pretends the graph paused

`ask_user:2100` · plus `report_or_respond:2513` prompt patch · plus `_gate_node:3690` collapse

**The jugaad.** Three coordinated hacks to simulate one pause:

1. `ask_user` emits an options event and returns a string telling the model
   "wait for the user's selection (it arrives as their next message)";
2. `report_or_respond` detects a trailing `ask_user` ToolMessage and patches the
   system prompt with "do NOT call any tool now";
3. `_gate_node` collapses any step containing `ask_user` to a single call.

The graph actually runs to `END` and the next user message re-enters cold.

**Native LangGraph.** `interrupt()` suspends the graph *inside the node*,
persists the checkpoint, and surfaces the payload. The turn resumes exactly where
it stopped when you call the graph again with `Command(resume=…)` — no prompt
patching, no "is this a real ask_user or a guard nudge" disambiguation, no gate
rule.

```python
from langgraph.types import interrupt, Command

def ask_user_node(state: CasperState):
    choice = interrupt({"questions": state["pending_options"]})
    return {"messages": [HumanMessage(content=choice)]}

# API layer, on the user's reply:
await graph.ainvoke(
    Command(resume=user_reply),
    config={"configurable": {"thread_id": chat_id}},
)
```

Requires the checkpointer (S1). Detect a waiting graph with
`graph.aget_state(config)` → `.interrupts`.

---

## Tool orchestration

Most of the `_gate_node` / `_sequential_tools_node` / routing machinery exists to
undo parallel tool calls after the fact. Disable them at the source and the
machinery collapses.

### T1 — Collapsing parallel tool calls by AIMessage rewrite

`_gate_node:3597–3727`

**The jugaad.** The gate inspects the last `AIMessage`, and when the model
emitted 2+ calls it constructs a replacement `AIMessage` with the *same id* so
the `add_messages` reducer overwrites it in place, keeping only the first
`retrieve` / first `ask_user`. A whole node plus a `_GATE_REJECTION_TEMPLATE`
plus `_normalize_tool_call` plumbing.

**Native LangGraph.** Anthropic and OpenAI both accept a flag to forbid parallel
tool calls. The model then emits at most one call per step and the gate's core
job disappears.

```python
llm_with_tools = self.anthropic_llm.bind_tools(
    bound_tools,
    parallel_tool_calls=False,
)
```

"`retrieve` must run alone" and "`ask_user` must run alone" are then
automatically true. Anything still needed (e.g. "no other call in the same step
as X") becomes a one-line check in routing, not a rewrite.

### T2 — Hand-rolled sequential tool dispatcher

`_sequential_tools_node:3823–3954` · `_dispatch_read_tool:3794`

**The jugaad.** ~130 lines re-implementing `ToolNode`: iterate the calls, match
names to methods by hand, wrap each in try/except, build a stubbed `ToolMessage`
for every skipped/failed call so provider history stays valid, enforce
`MAX_*_CALLS_PER_TURN` mid-loop.

**Native LangGraph.** With T1 in place there's rarely more than one call, so the
prebuilt `ToolNode` handles dispatch, error-to-ToolMessage conversion, and id
pairing. It even takes a `handle_tool_errors` callable for your generic-error
string.

```python
graph.add_node("tools", ToolNode(
    tools,
    handle_tool_errors=GENERIC_TOOL_ERROR_MSG,
))
```

Genuinely sequential multi-tool batches (if you keep them) are a `ToolNode`
subclass or a small loop that *calls* `ToolNode` per item — not a from-scratch
dispatcher.

### T3 — Per-turn call caps by message archaeology

`_count_turn_tool_calls:3562` · duplicated at `report_or_respond:2469–2506`

**The jugaad.** Two copies of "scan backwards to the last human message, count
`retrieve_latest_info` / `query_document` ToolMessages since then" to decide
which tools to bind and which calls to skip.

**Native LangGraph.** Keep the counts in state. A tiny pre-model node (or the
tool's own `Command` update) increments; a check at the top of the turn resets
them when the last message is human. Binding logic reads
`state["turn_tool_calls"]`.

```python
def prep_turn(state: CasperState):
    last = state["messages"][-1]
    if isinstance(last, HumanMessage):
        return {"turn_tool_calls": {}}
    return {}
```

---

## Control-flow constraints

### F1 — The layout-first ordering guard

`_LAYOUT_GUARD_MARKER` · `_gate_node:3637–3683` · `report_or_respond:2529–2546` · `_route_after_gate:3773–3787`

**The jugaad.** To force `propose_report_layout` before `ask_user`/`retrieve`:
the gate suppresses the premature calls and answers each with a `[layout-guard]`
nudge ToolMessage, bounded to 2 per turn then "fails open"; `report_or_respond`
injects two different system-prompt paragraphs; `_route_after_gate` has special
logic to detect "all pending calls already answered by nudges" and loop back.
Four sites, one rule.

**Native LangGraph.** A `layout_proposed` flag in state (set by the tool's
`Command`) plus **forced `tool_choice`** when it's false and the request is a
report. The model is structurally unable to call anything else.

```python
def plan_node(state: CasperState):
    tools = [retrieve, ask_user, propose_report_layout, ...]
    if is_report_request(state) and not state.get("layout_proposed"):
        llm = anthropic_llm.bind_tools(
            [propose_report_layout],
            tool_choice="propose_report_layout",
        )
    else:
        llm = anthropic_llm.bind_tools(tools, parallel_tool_calls=False)
```

No nudges, no marker string, no bounded-retry, no double prompt patch, no special
routing case. The one real fallback ("model still won't cooperate") is a single
conditional edge.

**Caveat:** forced `tool_choice` disables text output on that turn for Anthropic —
fine for the layout-first step (you want only the tool call), but don't force it
on steps where the model should also be able to answer.

### F2 — Background layout refresh outside the graph

`propose_report_layout:2254` · `_spawn_background_task` · `_await_layout_refresh:436` · `_patched_layout_tool_message:462`

**The jugaad.** `propose_report_layout` spawns `update_proposed_report_layout` as
a detached `asyncio` task (held in `self._background_tasks` so the GC doesn't eat
it). The graph then has to *wait for it* before closing
(`_await_layout_refresh`, 150 s timeout, cancel-on-overrun), and afterward
`_patched_layout_tool_message` reaches back into history and rewrites the earlier
`propose_report_layout` ToolMessage by reusing its id. The refresh streams
through a writer that may already be closed.

**Native LangGraph.** Make it a node. LangGraph runs independent branches
concurrently, so a fan-out from `plan_node` to both "respond" and
"refresh_layout" executes them in parallel and the framework owns the join — no
manual task set, no timeout babysitting, no writer-lifetime problem. If the
refresh should land even later, a deferred node (`defer=True`) runs after
everything else feeding it settles.

```python
builder.add_node("refresh_layout", self.refresh_layout_node)
builder.add_edge("propose_layout", "refresh_layout")
builder.add_edge("propose_layout", "respond")
# both run; refresh_layout writes the updated layout to state,
# the messages reducer overwrites by id — no reach-back helper
```

---

## Subgraphs & streaming

### P1 — Manual subgraph streaming bridge

`run_primary_research:4034` · `run_due_diligence:4127`

**The jugaad.** Each wrapper builds an initial dict from `self.retrieve_config`,
calls `self._pr_subgraph.astream(…, stream_mode=["values","custom"])` by hand,
and re-emits every `custom` event through the parent's `event_writer` — with a
comment explaining that "LangGraph isolates the subgraph's writer." Then it digs
`final_message` out of the final state.

**Native LangGraph.** Add the **compiled subgraph itself** as a node. When parent
and child share state keys, custom/messages events propagate automatically; the
parent just needs `stream(..., subgraphs=True)` to see them namespaced. The
wrapper's real job — schema translation — shrinks to a shared `BaseDomainState`
or a thin input/output transform.

```python
builder.add_node("pr_subgraph", self._pr_subgraph)   # compiled graph
builder.add_node("dd_subgraph", self._dd_subgraph)

# parent stream:
graph.astream(state, stream_mode=["messages", "custom"], subgraphs=True)
```

The reason it "isolates the writer" today is that the subgraph is wrapped in a
plain `async def` that starts a nested `astream`; a subgraph added as a node is
not isolated.

> **Optional, larger:** the card-generation fan-out in `generate_report`
> (producer/consumer over `asyncio.create_task` per card) is what the `Send` API
> is for — map a node over a list with per-item checkpointing and ordered
> streaming. Worth it only if card generation becomes a reliability problem; the
> streaming-order constraints there may justify keeping the custom loop.

---

## Resilience & fallback

### R1 — Nested provider fallback

`report_or_respond:2565–2583` · `generate_chat_title:4341–4440` (four nested levels)

**The jugaad.** `report_or_respond`: try Anthropic, `except` → try OpenAI,
`except` → raise. `generate_chat_title`: OpenAI strict-JSON → `except` →
LangChain `STRUCTURED_LLM` → `except` → OpenAI 5-word → `except` → Gemini →
`except` → string slice. Each level re-specifies the schema and re-parses.

**Native LangChain / LangGraph.** Compose once. `with_fallbacks` gives an ordered
chain that behaves like a single runnable; `with_structured_output` handles
schema + parse for all providers; a node `RetryPolicy` covers transient errors
without a loop.

```python
chat_llm = self.anthropic_llm.bind_tools(tools, parallel_tool_calls=False) \
    .with_fallbacks([openai_llm.bind_tools(tools)])

title_llm = anthropic_llm.with_structured_output(ChatTitle) \
    .with_fallbacks([openai_llm.with_structured_output(ChatTitle)])

builder.add_node("plan", self.plan_node,
                 retry=RetryPolicy(max_attempts=3))
```

> Two more small ones fall out of langchain-core 1.x: `_message_text()`
> (hand-parsing Anthropic content blocks) → the `msg.text` property; and the
> `_TRANSIENT_FLAG` / `_is_transient` / `_compact_history` machinery for hiding
> gate-correction messages → mostly unneeded once T1/H1 remove the corrections,
> and `RemoveMessage` where it isn't.

---

## Suggested sequencing

Each step is independently shippable and testable. Order matters — later steps
depend on the checkpointer and the state schema.

1. **Align dependencies.** Bump `requirements.txt` to `langgraph>=1.1,<2` and the
   langchain 1.x line already installed. Run the existing graph unchanged, fix
   any import breaks. No behaviour change.
2. **Disable parallel tool calls (T1).** Add `parallel_tool_calls=False` to the
   `bind_tools` calls. Delete the collapse branches of `_gate_node`; keep only
   any genuine routing checks. Verify `retrieve`/`ask_user` still run alone.
3. **Add the checkpointer (S1).** `AsyncPostgresSaver` keyed by `chat_id`. Delete
   the history-reconstruction block in `get_processing_state`. Keep your DB as
   the source of truth; the checkpointer is graph working memory.
4. **Introduce `CasperState` (S2, S3, T3).** Custom schema with `domain_name`,
   `retrieve_config`, `layout_proposed`, `turn_tool_calls`. Move tools to
   `Command(update=…)` returns and `InjectedState` args. Remove the matching
   `self.*` attributes.
5. **Native `ask_user` (H1).** Replace with an `interrupt()` node and a
   `Command(resume=…)` entry path in the API layer. Delete the prompt patch and
   the gate's ask_user branch.
6. **Forced `tool_choice` for layout-first (F1).** Drive it off
   `state["layout_proposed"]`. Delete `_LAYOUT_GUARD_MARKER`, the nudge logic,
   both prompt paragraphs, and the "already answered" routing case.
7. **Fold `_sequential_tools_node` into `ToolNode` (T2).** With T1 done, the
   prebuilt node covers it. Move per-turn caps to the `prep_turn` node.
8. **Subgraphs as nodes (P1).** Share `BaseDomainState` keys, add the compiled
   subgraphs directly, switch the parent stream to `subgraphs=True`. Delete the
   re-emit loops.
9. **Layout refresh as a node (F2).** The biggest change — do it last, once state
   and streaming are native. Fan out from the plan node; drop
   `_background_tasks`, `_await_layout_refresh`, `_patched_layout_tool_message`.
10. **Fallback combinators (R1).** Cosmetic once the rest lands — swap the
    try/except pyramids for `with_fallbacks` / `with_structured_output` /
    `RetryPolicy` at leisure.

---

## Risks & caveats — read before starting

- **Checkpointer storage growth.** Every turn writes a checkpoint. Configure a
  TTL / periodic delete, or a custom saver that prunes. Postgres saver needs its
  `setup()` migration run once.
- **Interrupt semantics changed.** A graph waiting on `interrupt()` is genuinely
  paused — your API layer must detect that state (`aget_state().interrupts`) and
  route the next user message to `Command(resume=…)` rather than a fresh invoke.
  Timeouts, abandoned chats, and "user sent something unrelated while we were
  waiting" all need explicit handling.
- **`parallel_tool_calls=False` is provider-enforced,** not a LangGraph feature —
  confirm the OpenAI and Gemini fallback paths honour it too (OpenAI: yes; check
  the Gemini client).
- **Forced `tool_choice` disables text output** on that turn for Anthropic — fine
  for the layout-first step (you want only the tool call), but don't force it on
  steps where the model should also be able to answer.
- **Concurrent access to one `thread_id`.** Two requests for the same chat now
  contend on checkpoint state instead of two independent `Casper` objects. Add a
  per-chat lock or rely on the saver's optimistic concurrency.
- **Streaming shape.** `subgraphs=True` namespaces events
  (`(namespace, mode, data)`) — the SSE adapter in `api.py` needs to handle the
  extra tuple element.
- **The gate isn't purely redundant.** Re-audit each rule before deleting: some
  encode product decisions ("report generation is exclusive") that still need a
  home, just a smaller one on a routing edge.
