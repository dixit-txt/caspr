#!/usr/bin/env python3
"""Interactive harness for the *chat-while-a-report-is-generating* feature.

    cd caspr-core && .venv/bin/python src/app/research/agent/try_respond_during_report.py [--raw]

It runs a REAL Casper graph (real LLM calls) but STUBS the expensive report
subgraphs, so a session is fast and safe to repeat. You drive it from a prompt
and watch, per turn:

  * which entry node handled the message  (respond_during_report vs report_or_respond)
  * every `post_report_edit_request` event, with its parsed payload
  * the streamed assistant reply

Commands (type at the ``you ▸`` prompt):

  /scenario            set up a ready-made "report generating" state (title + layout)
  /report on [title]   turn the in-progress-report flag ON  (routes to respond_during_report)
  /report off          turn it OFF                          (routes to report_or_respond)
  /title <text>        set the in-progress report title
  /layout              paste a proposed layout (end with a lone '.'); the node uses it
  /history             dump the running message history
  /state               show current flags
  /raw  (/events)      toggle a verbatim dump of every (mode, output) stream item
  /reset               start a fresh chat
  /help                show this list
  /quit                exit

Anything else is sent to the model as your next chat message.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pprint
import sys
import warnings

# ── run under the project venv, whatever interpreter launched us ────────────
# This harness needs the app's dependencies (langchain, langgraph, …). If it
# was started with some other Python (system 3.12, an IDE default, …), re-exec
# it with caspr-core/.venv so `python path/to/this_file.py` just works.
_VENV_PYTHON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    ))))),
    ".venv", "bin", "python",
)
if (
    os.path.exists(_VENV_PYTHON)
    and os.path.realpath(_VENV_PYTHON) != os.path.realpath(sys.executable)
    and not os.environ.get("_CASPR_HARNESS_REEXEC")
):
    os.environ["_CASPR_HARNESS_REEXEC"] = "1"
    os.execv(_VENV_PYTHON, [_VENV_PYTHON, os.path.abspath(__file__), *sys.argv[1:]])

warnings.filterwarnings("ignore")
warnings.showwarning = lambda *a, **k: None  # dependency import warnings — not ours
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# ── silence the app's chatty logging + the harness's expected infra errors ──
# The harness has no Postgres / S3 / CloudWatch, so the app's fire-and-forget
# writers (LLM cost tracker, analytics) fail on every call. Those failures are
# irrelevant here — drop every log record and asyncio task error that mentions
# the missing infrastructure, and mute the noisiest loggers outright.
logging.disable(logging.INFO)

_NOISE_MARKERS = (
    "costtracker", "cost tracker", "cost_tracker", "insert_cost",
    "5432", "asyncpg", "ConnectionRefused", "Connect call failed",
    "cloudwatch", "CloudWatch", "greenlet", "MissingGreenlet",
    "s3", "S3", "boto", "endpoint", "credentials",
    "Database session", "async_session_scope", "TargetServerAttributeNotMatched",
)


class _DropInfraNoise(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:
            text = str(getattr(record, "msg", ""))
        if record.exc_info and record.exc_info[1]:
            text += " " + repr(record.exc_info[1])
        return not any(m in text for m in _NOISE_MARKERS)


_DROP_FILTER = _DropInfraNoise()


def _quiet_app_logging() -> None:
    """Call AFTER importing the app (setup_logging resets levels at import time)."""
    warnings.filterwarnings("ignore")  # re-apply: deps install their own filters
    logging.disable(logging.INFO)
    logging.getLogger().addFilter(_DROP_FILTER)
    for name in ["", *list(logging.root.manager.loggerDict)]:
        lg = logging.getLogger(name)
        lg.addFilter(_DROP_FILTER)
        for h in list(getattr(lg, "handlers", [])):
            h.addFilter(_DROP_FILTER)
        if lg.level and lg.level < logging.WARNING:
            lg.setLevel(logging.WARNING)
    for loud in ("app.observability.llm_response_logger", "app.observability.web_search_analytics",
                 "app.observability.error_alerter", "app.observability.cloudwatch_utils"):
        logging.getLogger(loud).setLevel(logging.CRITICAL)


def _swallow_db_noise(loop, context):
    exc = context.get("exception")
    if isinstance(exc, (ConnectionRefusedError, OSError)):
        return
    blob = f"{context.get('message', '')} {exc!r}"
    if any(m in blob for m in _NOISE_MARKERS):
        return
    loop.default_exception_handler(context)


# Put `src/` on the path so `import app.*` resolves when this script is run
# directly (…/src/app/research/agent/ -> up 4 -> …/src).
sys.path.insert(
    0,
    os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ),
)

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from langchain_core.messages import AIMessage
        from langgraph.config import get_stream_writer

        from app.research.agent import model as casper_model
except Exception as exc:  # pragma: no cover
    print(f"\n  Could not import the app ({exc!r}).")
    print("  Run this from the caspr-core/ directory with its venv active and a .env present.\n")
    raise SystemExit(1)

_quiet_app_logging()


# ── tiny ANSI helpers (no third-party deps) ─────────────────────────────────
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def dim(t):     return _c(t, "2")
def bold(t):    return _c(t, "1")
def cyan(t):    return _c(t, "36")
def green(t):   return _c(t, "32")
def yellow(t):  return _c(t, "33")
def magenta(t): return _c(t, "35")
def red(t):     return _c(t, "31")


BANNER = f"""
{bold('respond_during_report — interactive harness')}
{dim('The report subgraphs are stubbed; the planner and the new node are real.')}

  {cyan('/scenario')}  set up a "report generating" state, then just start chatting
  {cyan('/report on|off')}   {cyan('/title <t>')}   {cyan('/layout')}   {cyan('/state')}   {cyan('/history')}
  {cyan('/raw')} (or {cyan('/events')})  dump every stream item verbatim   ·   start with {cyan('--raw')} to have it on
  {cyan('/reset')}     {cyan('/help')}     {cyan('/quit')}
"""

SAMPLE_LAYOUT = """# EV Market Outlook 2026

## Executive Summary
## Market Size and Growth
## Competitive Landscape (Tesla, BYD, VW, legacy OEMs)
## Battery Supply Chain
## Policy and Incentives
## Risks and Headwinds
## Outlook and Recommendations
"""


class Harness:
    def __init__(self, casper):
        self.casper = casper
        self.history: list = []          # list[dict]  (message dumps, no system msg)
        self.layout_text: str = ""
        self.show_raw = False

    # -- state helpers ----------------------------------------------------
    def set_report_in_progress(self, on: bool, title: str | None = None):
        self.casper.report_in_progress = on
        self.casper.in_progress_report_id = "harness-report-1" if on else None
        if title:
            self.casper.in_progress_report_title = title
        elif on and not getattr(self.casper, "in_progress_report_title", None):
            self.casper.in_progress_report_title = "Untitled report"

    def print_state(self):
        c = self.casper
        print(dim("  ┌─ state"))
        print(dim("  │  report_in_progress : ") + (green("ON") if c.report_in_progress else red("off")))
        print(dim(f"  │  report_id          : {c.in_progress_report_id}"))
        print(dim(f"  │  report_title       : {c.in_progress_report_title!r}"))
        print(dim(f"  │  layout set         : {'yes' if self.layout_text else 'no'} ({len(self.layout_text)} chars)"))
        print(dim(f"  │  history messages   : {len(self.history)}"))
        print(dim(f"  │  raw events         : {'on' if self.show_raw else 'off'}"))
        print(dim("  └─"))

    def dump_history(self):
        if not self.history:
            print(dim("  (empty)"))
            return
        for i, m in enumerate(self.history):
            t = m.get("type")
            name = m.get("name")
            tcs = m.get("tool_calls") or []
            content = m.get("content")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
            content = (content or "").replace("\n", " ")
            tag = t if not name else f"{t}:{name}"
            extra = f"  tool_calls={[tc.get('name') for tc in tcs]}" if tcs else ""
            print(dim(f"  [{i:>2}] {tag:<18} ") + content[:110] + extra)

    # -- the layout the node references --------------------------------
    def install_layout_hook(self):
        """`respond_during_report` reads the proposed layout via
        `_extract_in_progress_layout`. Point it at whatever the user pasted."""
        harness = self

        def _extract(_messages):
            return harness.layout_text.strip()[:6000]

        self.casper._extract_in_progress_layout = _extract

    # -- one turn -----------------------------------------------------
    async def send(self, text: str):
        self.casper.user_previous_messages = list(self.history)
        gs = await self.casper.get_processing_state(text)

        nodes_seen: list[str] = []
        edit_events: list[dict] = []
        assistant_started = False
        streamed_any_text = False
        final_messages = None

        def _end_stream():
            nonlocal assistant_started
            if assistant_started:
                sys.stdout.write("\n")
                assistant_started = False

        async for mode, output in gs:
            if mode == "messages":
                chunk, meta = output
                node = meta.get("langgraph_node", "")
                is_ai = getattr(chunk, "type", "") == "AIMessageChunk"
                # per-token chunks are intentionally NOT raw-dumped — they'd bury
                # the events; the live "caspr ▾" stream already shows the text.
                if node in ("report_or_respond", "respond_during_report") and is_ai:
                    piece = _chunk_text(chunk)
                    if piece:
                        if not assistant_started:
                            sys.stdout.write("\n" + green("caspr ▾ "))
                            assistant_started = True
                        sys.stdout.write(piece)
                        sys.stdout.flush()
                        streamed_any_text = True

            elif mode == "updates":
                _end_stream()
                for node_name, payload in output.items():
                    nodes_seen.append(node_name)
                    if self.show_raw:
                        _print_raw("updates", {node_name: _dump_msgs(payload)})

            elif mode == "custom":
                name = output.get("name", "")
                status = output.get("status", "")
                if self.show_raw:
                    _end_stream()
                    _print_raw("custom", output)
                if name == "post_report_edit_request":
                    _end_stream()
                    edit_events.append(output)
                    _print_edit_event(output)
                elif self.show_raw:
                    pass  # already dumped above
                elif name == "ask_user" and status == "options":
                    questions = output.get("questions", []) or []
                    if questions:
                        _end_stream()
                        print(yellow("\n  ⁇ ask_user — options presented:"))
                        for i, q in enumerate(questions, 1):
                            print(yellow(f"      {i}. {q}"))
                elif name == "propose_report_layout" and "report_layout" in output:
                    _end_stream()
                    print(magenta(f"\n  ▤ propose_report_layout  «{output.get('report_title','')}»"))
                elif name == "retrieve":
                    _end_stream()
                    print(magenta(f"\n  ⟳ retrieve → report generation would start ({_slim(output)})"))
                elif name in ("generate_report", "pr_subgraph", "dd_subgraph") and status == "card_stream_start":
                    _end_stream()
                    print(magenta("\n  ▤ (stubbed) report subgraph running…"))
                elif status and name not in ("report_or_respond", "respond_during_report"):
                    _end_stream()
                    print(dim(f"\n  · {name or 'event'}: {status}"))

            elif mode == "values":
                final_messages = output.get("messages")
                if self.show_raw:
                    msgs = output.get("messages") or []
                    _print_raw("values", f"{len(msgs)} messages in state "
                                          f"(last: {getattr(msgs[-1], 'type', '?') if msgs else '-'})")

        _end_stream()

        # Fallback: some replies never stream as tokens (a stubbed report node, a
        # message a subgraph returns whole). Show the last AI message so a turn is
        # never silent.
        if not streamed_any_text and final_messages:
            last_ai = next(
                (m for m in reversed(final_messages) if getattr(m, "type", None) == "ai"), None
            )
            body = _chunk_text(last_ai) if last_ai is not None else ""
            if body.strip():
                print("\n" + green("caspr ▾ ") + body)

        # routing verdict
        entry = next((n for n in nodes_seen if n in ("respond_during_report", "report_or_respond")), "?")
        badge = green(entry) if entry == "respond_during_report" else yellow(entry)
        path = dim(" → ".join(nodes_seen)) if nodes_seen else dim("(no nodes?)")
        print(dim("  ─ handled by ") + badge + dim("   path: ") + path)
        if edit_events:
            print(bold(green(f"  ★ {len(edit_events)} post_report_edit_request event(s) emitted this turn")))

        # carry the conversation forward
        if final_messages:
            self.history = [
                m.model_dump() for m in final_messages
                if getattr(m, "type", None) != "system"
            ]


def _chunk_text(chunk) -> str:
    c = getattr(chunk, "content", "")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for part in c:
            if isinstance(part, dict) and part.get("type") in (None, "text") and "text" in part:
                out.append(part["text"])
        return "".join(out)
    return ""


def _slim(d: dict) -> str:
    return ", ".join(f"{k}={v!r}"[:60] for k, v in d.items() if k not in ("name", "status"))


def _print_raw(mode: str, payload) -> None:
    """Dump a stream item verbatim (pretty JSON where possible)."""
    try:
        body = json.dumps(payload, indent=2, default=str, ensure_ascii=False)
    except Exception:
        body = pprint.pformat(payload, width=100)
    head = dim(f"  ┄ [{mode}] ")
    lines = body.splitlines() or [body]
    print(head + dim(lines[0]))
    for ln in lines[1:]:
        print(dim("      " + ln))


def _dump_msgs(payload):
    """Turn a {'messages': [...]} node output into plain dicts for raw display."""
    if isinstance(payload, dict) and "messages" in payload:
        out = []
        for m in payload["messages"]:
            if hasattr(m, "model_dump"):
                d = m.model_dump()
                out.append({k: d[k] for k in ("type", "name", "content", "tool_calls", "tool_call_id")
                            if k in d and d[k] not in (None, [], "")})
            else:
                out.append(m)
        return {"messages": out}
    return payload


def _print_edit_event(evt: dict):
    # Current event shape: {"name", "status", "chat_id", "report_id", "request": <str>}
    # where `request` is the user's verbatim change request. (Older builds emitted
    # a structured dict — still rendered if that turns up.)
    req = evt.get("request")
    print()
    print(bold(magenta("  ╔══ post_report_edit_request ═══════════════════════════════")))
    print(magenta("  ║ ") + dim("status    ") + str(evt.get("status", "")))
    print(magenta("  ║ ") + dim("chat_id   ") + str(evt.get("chat_id")))
    print(magenta("  ║ ") + dim("report_id ") + str(evt.get("report_id")))
    print(magenta("  ║ ") + dim("request"))
    if isinstance(req, dict):
        for k, v in req.items():
            print(magenta("  ║   ") + dim(f"{k}: ") + str(v))
    else:
        for line in (str(req or "").splitlines() or [""]):
            print(magenta("  ║   ") + line)
    print(bold(magenta("  ╚═══════════════════════════════════════════════════════════")))


def _read_block(prompt: str) -> str:
    print(dim(prompt + " (end with a single '.' on its own line)"))
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == ".":
            break
        lines.append(line)
    return "\n".join(lines)


async def _stub_report_node(state):
    """Stands in for generate_report / run_primary_research / run_due_diligence
    so a harness session never triggers a real multi-minute build."""
    try:
        w = get_stream_writer()
        w({"name": "generate_report", "status": "card_stream_start", "title": "Harness stub"})
        w({"name": "generate_report", "status": "card_stream_complete"})
        w({"name": "generate_report", "status": "md_content", "md_content": "# Stub\n(no real report generated)"})
    except Exception:
        pass
    return {"messages": [AIMessage(content="(harness stub) a real report would be generated here.")]}


def _make_stub_retrieve(casper):
    """Stub for Casper.retrieve — kicks off report generation. Keeps the real
    tool's exact signature so its LLM schema is unchanged, but skips the real
    S3 / context-gathering work so the harness stays fast and offline."""

    async def retrieve(
        user_instructions: str,
        report_layout: str,
        report_language: str,
        report_title: str,
        domain_name: str,
        report_type: str = "study",
    ):
        """Retrieve information per user_instructions and generate the report
        according to report_layout. Pass domain_name explicitly every call."""
        # The Confirm submission is the only tier authority (R10). The stub
        # has to honour it the same way the real retrieve does, or a harness
        # resume would still generate the model's guessed tier.
        confirmed = getattr(casper, "report_config_submission", None) or {}
        if confirmed.get("report_tier"):
            report_type = confirmed["report_tier"]
        if confirmed.get("style"):
            casper.card_style = confirmed["style"]
        confirmed_layout = (getattr(casper, "report_layout_pair", None) or {}).get(report_type)
        if confirmed_layout:
            report_layout = confirmed_layout.get("markdown") or report_layout
            report_title = confirmed_layout.get("report_title") or report_title
        try:
            w = get_stream_writer()
            w({"name": "retrieve", "report_title": report_title, "report_type": report_type})
            w({"name": "retrieve", "domain_name": domain_name})
            w({"name": "retrieve", "report_layout": report_layout})
        except Exception:
            pass
        casper.domain_name = domain_name
        casper.retrieve_config.update({
            "user_instructions": user_instructions,
            "report_layout": report_layout,
            "report_language": "English",
            "report_title": report_title,
            "report_type": report_type,
            "domain_name": domain_name,
            **{k: v for k, v in confirmed.items() if k},
        })
        # A report is now generating. In production caspr-backend flips this flag
        # (report status -> ANALYSIS_IN_PROGRESS) on the next chat invocation;
        # the harness has no backend, so simulate it here so follow-up turns
        # route to respond_during_report the way they would live. `/report off`
        # clears it.
        casper.report_in_progress = True
        casper.in_progress_report_id = "harness-report-1"
        casper.in_progress_report_title = report_title
        return ("(harness) retrieved context stub — proceeding to report generation.", [])

    return retrieve


def _silence_side_effects():
    """The harness has no Postgres — neutralise the fire-and-forget writers
    (LLM cost tracker, raw-response logger) so nothing tries to hit a DB."""
    casper_model.save_raw_llm_response = lambda *a, **k: None
    try:
        import app.observability.llm_response_logger as _llmlog

        _llmlog.save_raw_llm_response = lambda *a, **k: None
        if hasattr(_llmlog, "track_llm_cost"):
            _llmlog.track_llm_cost = lambda *a, **k: None

        async def _noop_async(*a, **k):
            return None

        for fn in ("_persist_cost_tracker_row", "_persist_cost_tracker_row_isolated"):
            if hasattr(_llmlog, fn):
                setattr(_llmlog, fn, _noop_async)
    except Exception:
        pass


async def build_casper():
    _silence_side_effects()
    cfg = {
        "user_name": "harness",
        "chat_id": "harness-chat-1",
        "user_previous_messages": [],
        "user_id": "harness-user",
    }
    casper = casper_model.Casper(cfg)
    # stub the heavy work BEFORE the graph captures the bound methods
    casper.generate_report = _stub_report_node
    casper.run_primary_research = _stub_report_node
    casper.run_due_diligence = _stub_report_node
    casper.retrieve = _make_stub_retrieve(casper)
    await casper.async_init()
    return casper


async def main():
    asyncio.get_running_loop().set_exception_handler(_swallow_db_noise)
    print(dim("building casper (real LLM clients, stubbed report subgraphs)…"))
    casper = await build_casper()
    h = Harness(casper)
    h.install_layout_hook()
    h.set_report_in_progress(False)
    if any(a in ("--raw", "-r", "--events") for a in sys.argv[1:]):
        h.show_raw = True
        print(dim("  raw events: on  (started with --raw)"))
    print(BANNER)

    while True:
        try:
            raw = input(bold(cyan("\nyou ▸ ")))
        except (EOFError, KeyboardInterrupt):
            print()
            break

        text = raw.strip()
        if not text:
            continue

        if text in ("/quit", "/q", "/exit"):
            break
        if text in ("/help", "/h", "/?"):
            print(BANNER)
            continue
        if text == "/state":
            h.print_state()
            continue
        if text == "/history":
            h.dump_history()
            continue
        if text in ("/raw", "/events"):
            h.show_raw = not h.show_raw
            print(dim(f"  raw events: {'on' if h.show_raw else 'off'}"))
            continue
        if text == "/reset":
            h.history = []
            print(dim("  chat reset"))
            continue
        if text == "/scenario":
            h.layout_text = SAMPLE_LAYOUT
            h.set_report_in_progress(True, "EV Market Outlook 2026")
            h.history = []
            print(green("  scenario ready: report 'EV Market Outlook 2026' is 'generating'."))
            print(dim("  try:  add a paragraph on charging infrastructure to the Risks section"))
            print(dim("  try:  how many sections does the report have?   (no event expected)"))
            continue
        if text.startswith("/report"):
            parts = text.split(maxsplit=2)
            if len(parts) >= 2 and parts[1] == "on":
                h.set_report_in_progress(True, parts[2] if len(parts) > 2 else None)
                print(green(f"  report_in_progress = ON  (title {h.casper.in_progress_report_title!r})"))
            elif len(parts) >= 2 and parts[1] == "off":
                h.set_report_in_progress(False)
                print(yellow("  report_in_progress = off"))
            else:
                print(dim("  usage: /report on [title]   |   /report off"))
            continue
        if text.startswith("/title"):
            h.casper.in_progress_report_title = text[len("/title"):].strip() or h.casper.in_progress_report_title
            print(dim(f"  title = {h.casper.in_progress_report_title!r}"))
            continue
        if text == "/layout":
            h.layout_text = _read_block("  paste the proposed layout")
            print(dim(f"  layout stored ({len(h.layout_text)} chars)"))
            continue
        if text.startswith("/"):
            print(red(f"  unknown command {text!r} — /help"))
            continue

        try:
            await h.send(text)
        except Exception as exc:
            import traceback
            print(red(f"\n  turn failed: {exc!r}"))
            if h.show_raw:
                traceback.print_exc()

    print(dim("bye"))


if __name__ == "__main__":
    asyncio.run(main())
