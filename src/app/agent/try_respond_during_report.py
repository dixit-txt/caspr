#!/usr/bin/env python3
"""Interactive harness for *chat-while-a-report-is-generating* + the refine router.

    cd caspr-core && .venv/bin/python src/app/agent/try_respond_during_report.py [--raw]

It runs a REAL Casper graph (real LLM calls) but STUBS the expensive report
subgraphs, so a session is fast and safe to repeat. On top of that it wires in a
REAL ``RefineRequestRouter`` (``src/app/research/agent/refine_request_router.py``) so you
can watch the whole in-progress-edit pipeline end to end:

  1. ``/scenario`` — a report "starts generating": its section cards are streamed
     one by one and each is fed into the RefineRequestRouter (you see it land).
  2. You keep chatting. Ask for a change to the report.
  3. ``respond_during_report`` emits a ``post_report_edit_request`` event — shown.
  4. The harness feeds that event into the RefineRequestRouter — shown.
  5. The router's routing LLM decides which section you meant and produces
     ``fe_json_for_refine`` (the payload the refiner takes) — shown.

Commands (type at the ``you ▸`` prompt):

  /scenario            report "EV Market Outlook 2026" starts generating: streams
                       cards into the RefineRequestRouter, flips the flag on
  /generate            re-stream the stub cards into the router (again)
  /cards               dump what the RefineRequestRouter is tracking
  /route <text>        route a change request through the router directly
                       (skips the chat turn — pure router test)
  /report on [title]   flip the in-progress-report flag ON  (→ respond_during_report)
  /report off          flip it OFF                           (→ report_or_respond)
  /title <text>        set the in-progress report title
  /layout              paste a proposed layout (end with a lone '.')
  /history             dump the running message history
  /state               show current flags + router status
  /raw  (/events)      toggle a verbatim dump of every (mode, output) stream item
  /reset               start a fresh chat (and a fresh router)
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
import re
import sys
import warnings

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
    for loud in ("app.observability.llm_response_logger",
                 "app.observability.web_search_analytics",
                 "app.observability.error_alerter",
                 "app.observability.cloudwatch_utils"):
        logging.getLogger(loud).setLevel(logging.CRITICAL)


def _swallow_db_noise(loop, context):
    exc = context.get("exception")
    if isinstance(exc, (ConnectionRefusedError, OSError)):
        return
    blob = f"{context.get('message', '')} {exc!r}"
    if any(m in blob for m in _NOISE_MARKERS):
        return
    loop.default_exception_handler(context)


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, _REPO_ROOT)

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from uuid import uuid7

        from langchain_core.messages import AIMessage
        from langgraph.config import get_stream_writer
        from app.research.agent import model as casper_model
        from app.research.agent import refine_request_router as refine_router_mod
        from app.research.agent.refine_request_router import RefineRequestRouter
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
def blue(t):    return _c(t, "34")
def red(t):     return _c(t, "31")


BANNER = f"""
{bold('respond_during_report + RefineRequestRouter — interactive harness')}
{dim('Report subgraphs are stubbed; the planner, respond_during_report node and the router are real.')}

  {cyan('/scenario')}  start a report "generating" — streams its cards into the router
  {cyan('/cards')}     what the router is tracking     {cyan('/route <text>')}  route a request directly
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


def _card_event(name: str, content: str, subs: list[tuple[str, str]] | None = None,
                card_type: str = "section") -> dict:
    """Build one `generate_report` card event in the exact shape model.py emits
    and refine_request_router.parse_card_event() consumes."""
    sub_sections = [
        {"id": str(uuid7()), "name": sname, "content": scontent,
         "tables": [{"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}],
         "summary": ""}
        for sname, scontent in (subs or [])
    ] or [{"id": str(uuid7()), "name": "", "content": "",
           "tables": [{"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}],
           "summary": ""}]
    return {
        "name": "generate_report",
        "card_type": card_type,
        "card_db": {
            "section": [{
                "id": str(uuid7()),
                "name": name,
                "content": content,
                "tables": [{"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}],
                "summary": "",
            }],
            "sub_sections": sub_sections,
            "citations": {},
            "summary": "",
        },
    }


def _cards_from_layout(layout_text: str) -> list[dict]:
    """Turn a proposed-layout markdown blob (## sections, ### subsections) into
    stub `generate_report` card events. This is what makes the harness coherent:
    the router then tracks the SAME sections the model actually planned, so
    "make the 2nd section shorter" resolves against the real report."""
    sections: list[tuple[str, list[str]]] = []
    for line in (layout_text or "").splitlines():
        s = line.strip()
        if s.startswith("### ") and sections:
            sections[-1][1].append(s[4:].strip())
        elif s.startswith("## "):
            name = re.sub(r"\s*\(.*\)\s*$", "", s[3:].strip())  # drop trailing "(…)"
            sections.append((name, []))
    if not sections:
        return _build_stub_cards()
    return [
        _card_event(
            name,
            f"(stub) Generated content for the '{name}' section would appear here — "
            f"a few paragraphs of analysis, figures and citations.",
            subs=[(sn, f"(stub) content for the '{sn}' subsection.") for sn in subs],
        )
        for name, subs in sections
    ]


def _build_stub_cards() -> list[dict]:
    """A believable set of section cards for 'EV Market Outlook 2026'."""
    return [
        _card_event(
            "Executive Summary",
            "Global EV sales reach 21M units in 2026, ~26% of new-car sales, with China "
            "and Europe leading and the US accelerating on new incentives.",
        ),
        _card_event(
            "Market Size and Growth",
            "The market is worth roughly $980B in 2026, growing ~18% YoY. Passenger BEVs "
            "dominate; commercial EV adoption lags but is inflecting in China.",
            subs=[
                ("Regional breakdown", "China ~58% of volume, Europe ~22%, North America ~12%, rest-of-world ~8%."),
                ("Segment mix", "SUVs and crossovers are ~61% of BEV sales; sub-$30k models are the fastest-growing tier."),
            ],
        ),
        _card_event(
            "Competitive Landscape",
            "BYD leads global BEV volume ahead of Tesla; VW Group is the strongest legacy "
            "OEM. Chinese exporters pressure European incumbents on price.",
            subs=[
                ("Tesla", "Margin compression continues as ASPs fall; energy and FSD are the growth story."),
                ("BYD", "Vertical integration on cells and power electronics underpins a durable cost lead."),
                ("Legacy OEMs", "VW, Hyundai-Kia and GM are narrowing the software gap but still trail on cost."),
            ],
        ),
        _card_event(
            "Battery Supply Chain",
            "LFP is now ~45% of cells shipped. Lithium prices are off their 2022 highs; "
            "sodium-ion enters low-end packs in volume for the first time.",
        ),
        _card_event(
            "Policy and Incentives",
            "US IRA credits remain the swing factor for North American demand; the EU 2035 "
            "ICE phase-out holds; China shifts from purchase subsidies to infrastructure.",
        ),
        _card_event(
            "Risks and Headwinds",
            "Charging build-out still trails vehicle growth in several markets, raw-material "
            "concentration persists, and a tariff escalation could fragment supply chains.",
        ),
        _card_event(
            "Outlook and Recommendations",
            "Base case: 30M units by 2028. Prioritise LFP/sodium supply deals, software "
            "talent, and markets with committed charging investment.",
        ),
    ]


class Harness:
    def __init__(self, casper):
        self.casper = casper
        self.history: list = []          # list[dict]  (message dumps, no system msg)
        self.layout_text: str = ""
        self.show_raw = False
        self.router: RefineRequestRouter | None = None
        self._router_events: list[dict] = []  # refine_request_routed events, newest last

    # -- router ---------------------------------------------------------
    def _router_sink(self, payload: dict) -> None:
        """event_writer handed to the RefineRequestRouter — the router emits its
        `refine_request_routed` custom event here."""
        self._router_events.append(payload)

    def ensure_router(self) -> RefineRequestRouter:
        if self.router is None:
            self.router = RefineRequestRouter(
                chat_id=self.casper.chat_id,
                report_id=self.casper.in_progress_report_id or "harness-report-1",
                report_title=getattr(self.casper, "in_progress_report_title", "") or "",
                user_id="harness-user",
                event_writer=self._router_sink,
            )
            print(blue(f"  ⟐ RefineRequestRouter created  (report_id={self.router.report_id})"))
        return self.router

    async def feed_card_to_router(self, event: dict, *, announce: bool = True) -> None:
        router = self.ensure_router()
        card = event.get("card_db") or {}
        section = (card.get("section") or [{}])[0]
        name = section.get("name") or event.get("card_type") or "?"
        await router.ingest_card_event(event)
        tracked = await router.tracked_cards()
        if announce:
            print(blue(f"    card → RefineRequestRouter: ") + bold(str(name))
                  + dim(f"   (tracking {len(tracked)} card(s))"))

    async def stream_stub_report(self) -> None:
        """Simulate the report subgraph streaming its cards, feeding each into
        the RefineRequestRouter as model.py would."""
        self.ensure_router()
        print(magenta("\n  ▤ report is generating — streaming section cards…"))
        for event in _cards_from_layout(self.layout_text):
            section = (event["card_db"]["section"] or [{}])[0]
            print(magenta(f"  ▸ card emitted: ") + bold(section["name"]))
            await self.feed_card_to_router(event)
            await asyncio.sleep(0.15)  # let it feel live
        tracked = await self.router.tracked_cards()
        print(green(f"  ✓ report cards streamed — router is tracking {len(tracked)} section(s)"))

    async def route_request_directly(self, text: str) -> None:
        router = self.ensure_router()
        print(blue(f"\n  → RefineRequestRouter.route_edit_request({text!r})"))
        routed = await router.route_edit_request(text)
        self._print_router_output(text, routed)

    async def dump_router_cards(self) -> None:
        if self.router is None:
            print(dim("  (no router yet — run /scenario)"))
            return
        cards = await self.router.tracked_cards()
        if not cards:
            print(dim("  (router is tracking no cards)"))
            return
        print(dim(f"  ┌─ RefineRequestRouter tracked cards ({len(cards)})"))
        for c in cards:
            print(dim("  │ ") + bold(c["section_name"] or c["card_type"])
                  + dim(f"   id={c['section_id'][:8] or '—'}  type={c['card_type']}"))
            snippet = (c["section_content"] or "").replace("\n", " ")
            if snippet:
                print(dim("  │   ") + snippet[:96])
            for s in c["subsections"]:
                print(dim("  │   • ") + (s["name"] or "—") + dim(f"  id={s['id'][:8]}"))
        print(dim("  └─"))

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
        print(dim("  │  router             : ") + (blue("live") if self.router else dim("not created")))
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
        card_events: list[dict] = []
        assistant_started = False
        streamed_any_text = False
        final_messages = None
        retrieve_fired = False
        retrieve_title: str | None = None
        retrieve_layout: str | None = None

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
                    # → feed the captured request into the RefineRequestRouter
                    print(blue("\n  → feeding this event into RefineRequestRouter"
                               ".handle_post_report_edit_request()"))
                    routed = await self.ensure_router().handle_post_report_edit_request(output)
                    self._print_router_output(str(output.get("request", "")), routed)

                elif name == "generate_report" and output.get("card_db"):
                    _end_stream()
                    card_events.append(output)
                    await self.feed_card_to_router(output)

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
                    self.layout_text = output["report_layout"]  # node references this
                    print(magenta(f"\n  ▤ propose_report_layout  «{output.get('report_title','')}»"))
                elif name == "retrieve":
                    _end_stream()
                    retrieve_fired = True
                    if output.get("report_title"):
                        retrieve_title = output["report_title"]
                    if output.get("report_layout"):
                        retrieve_layout = output["report_layout"]
                    print(magenta(f"\n  ⟳ retrieve → report generation started ({_slim(output)})"))
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
        if card_events:
            print(dim(f"  · {len(card_events)} card event(s) fed to the router this turn"))
        if edit_events:
            print(bold(green(f"  ★ {len(edit_events)} post_report_edit_request event(s) emitted "
                             f"& routed this turn")))

        # carry the conversation forward
        if final_messages:
            self.history = [
                m.model_dump() for m in final_messages
                if getattr(m, "type", None) != "system"
            ]

        # A `retrieve` call this turn kicked off report generation. In the real
        # system the API layer then builds the NEXT Casper with
        # report_in_progress=True (report status ANALYSIS_IN_PROGRESS), so
        # follow-up messages route to `respond_during_report`. The harness reuses
        # one Casper, so flip the flag here.
        if retrieve_fired and not self.casper.report_in_progress:
            if retrieve_layout:
                self.layout_text = retrieve_layout
            self.set_report_in_progress(True, retrieve_title or self.casper.in_progress_report_title)
            router = self.ensure_router()
            router.report_title = self.casper.in_progress_report_title or ""
            print()
            print(bold(green("  ══ report generation started — report_in_progress = ON ══")))
            print(green("  follow-up messages now route to ") + bold("respond_during_report")
                  + green("."))
            print(green("  ask for a change (e.g. \"make the 2nd section shorter\") to see:"))
            print(dim("    · the post_report_edit_request event"))
            print(dim("    · it fed into RefineRequestRouter"))
            print(dim("    · the refine_request_routed event it produces"))

    # -- render the router's output ------------------------------------
    def _print_router_output(self, raw_message: str, routed: dict | None):
        emitted = self._router_events[-1] if self._router_events else None
        print()
        print(bold(blue("  ╔══ RefineRequestRouter output ════════════════════════════")))
        print(blue("  ║ ") + dim("emitted event   ") + (green("refine_request_routed") if emitted else red("(none)")))
        if routed is None:
            print(blue("  ║ ") + red("router returned None (empty request?)"))
            print(bold(blue("  ╚═════════════════════════════════════════════════════════")))
            return
        target = routed.get("target", {}) or {}
        print(blue("  ║ ") + dim("status          ") + bold(str(routed.get("status"))))
        print(blue("  ║ ") + dim("raw_user_message"))
        for line in (raw_message or "").splitlines() or [""]:
            print(blue("  ║   ") + line)
        print(blue("  ║ ") + dim("─ LLM routed to ─"))
        print(blue("  ║ ") + dim("kind         ") + bold(str(target.get("kind"))))
        print(blue("  ║ ") + dim("matched_name ") + bold(str(target.get("matched_name") or "—")))
        print(blue("  ║ ") + dim("section_id   ") + str(target.get("section_id") or "—"))
        print(blue("  ║ ") + dim("subsection_id") + " " + str(target.get("subsection_id") or "—"))
        conf = target.get("confidence")
        print(blue("  ║ ") + dim("confidence   ") + (f"{conf:.2f}" if isinstance(conf, (int, float)) else str(conf)))
        if target.get("reasoning"):
            print(blue("  ║ ") + dim("reasoning    ") + str(target.get("reasoning")))
        print(blue("  ║ ") + dim("─ fe_json_for_refine (what the refiner takes) ─"))
        body = json.dumps(routed.get("fe_json_for_refine", {}), indent=2, ensure_ascii=False)
        for line in body.splitlines():
            print(blue("  ║   ") + line)
        print(bold(blue("  ╚═════════════════════════════════════════════════════════")))


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
    """`respond_during_report` → post_report_edit_request. `request` is the
    user's verbatim message (a string; older dict shape tolerated)."""
    request = evt.get("request", "")
    if isinstance(request, dict):
        request = request.get("raw_user_message", "")
    print()
    print(bold(magenta("  ╔══ post_report_edit_request  (from respond_during_report) ══")))
    print(magenta("  ║ ") + dim("chat_id   ") + str(evt.get("chat_id")))
    print(magenta("  ║ ") + dim("report_id ") + str(evt.get("report_id")))
    print(magenta("  ║ ") + dim("status    ") + str(evt.get("status")))
    print(magenta("  ║ ") + dim("request   ") + bold(str(request)))
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


def _make_stub_report_node(casper):
    """Stands in for generate_report / run_primary_research / run_due_diligence
    so a harness session never triggers a real multi-minute build. Streams stub
    section cards derived from the layout the model actually proposed (in
    `casper.retrieve_config`), so the RefineRequestRouter tracks the real
    report's sections."""

    async def _stub_report_node(state):
        layout = (getattr(casper, "retrieve_config", None) or {}).get("report_layout", "")
        title = (getattr(casper, "retrieve_config", None) or {}).get("report_title", "Harness stub")
        try:
            w = get_stream_writer()
            w({"name": "generate_report", "status": "card_stream_start", "title": title})
            for event in _cards_from_layout(layout):
                w(event)
            w({"name": "generate_report", "status": "card_stream_complete"})
            w({"name": "generate_report", "status": "md_content",
               "md_content": "# Stub\n(no real report generated)"})
        except Exception:
            pass
        return {"messages": [AIMessage(content="(harness stub) a real report would be generated here.")]}

    return _stub_report_node


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
        try:
            w = get_stream_writer()
            w({"name": "retrieve", "report_title": report_title})
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
        })
        return ("(harness) retrieved context stub — proceeding to report generation.", [])

    return retrieve


def _silence_side_effects():
    """The harness has no Postgres — neutralise the fire-and-forget writers
    (LLM cost tracker, raw-response logger) so nothing tries to hit a DB."""
    casper_model.save_raw_llm_response = lambda *a, **k: None
    refine_router_mod.save_raw_llm_response = lambda *a, **k: None
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
    stub_report_node = _make_stub_report_node(casper)
    casper.generate_report = stub_report_node
    casper.run_primary_research = stub_report_node
    casper.run_due_diligence = stub_report_node
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
        if text == "/cards":
            await h.dump_router_cards()
            continue
        if text.startswith("/route"):
            payload = text[len("/route"):].strip()
            if not payload:
                print(dim("  usage: /route add a paragraph on charging infra to the risks section"))
            else:
                await h.route_request_directly(payload)
            continue
        if text == "/generate":
            if not h.casper.report_in_progress:
                print(yellow("  (report_in_progress is off — turning it on)"))
                h.set_report_in_progress(True, h.casper.in_progress_report_title)
            await h.stream_stub_report()
            continue
        if text in ("/raw", "/events"):
            h.show_raw = not h.show_raw
            print(dim(f"  raw events: {'on' if h.show_raw else 'off'}"))
            continue
        if text == "/reset":
            h.history = []
            h.router = None
            h._router_events.clear()
            print(dim("  chat + router reset"))
            continue
        if text == "/scenario":
            h.layout_text = SAMPLE_LAYOUT
            h.set_report_in_progress(True, "EV Market Outlook 2026")
            h.history = []
            h.router = None
            h._router_events.clear()
            print(green("  scenario: report 'EV Market Outlook 2026' is now generating."))
            await h.stream_stub_report()
            print(dim("\n  now just chat. try:"))
            print(dim("    add a paragraph on charging infrastructure to the Risks section"))
            print(dim("    make the Tesla part shorter and less bullish"))
            print(dim("    how many sections does the report have?   (no edit event expected)"))
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
