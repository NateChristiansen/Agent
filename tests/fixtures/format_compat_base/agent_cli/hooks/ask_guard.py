# ruff: noqa
from __future__ import annotations
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from .. import sessions
from ..records.narrow import as_dict, as_int, as_str, dict_list

if TYPE_CHECKING:
    from markdown_it.token import Token
STATE = "ask_guard"
KEEP = 8
REGISTERED_CHARS = 4000
_URL = re.compile("(?:https?://|www\\.)\\S+")
_QUOTED = re.compile('\\"[^\\"]{1,1000}\\"|“[^”]{1,1000}”|«[^»]{1,1000}»')
_LIST_ITEM = re.compile("^\\s*(?:[-*+•]|\\d{1,2}[.)]|\\(?[a-zA-Z]\\))\\s+")
_EMPHASIS = re.compile("[*_~]+")
_SENTENCE_END = re.compile("(?P<run>[.?!]+)[)\\]\\\"'”’»]*(?P<space>\\s+)")
_ABBREVIATIONS = frozenset(
    "e.g. i.e. etc. vs. cf. approx. incl. esp. resp. viz. al. mr. mrs. ms. dr. st. no. nos. fig. ref. sec. ch. vol. p. pp. ed. est. min. max. jan. feb. mar. apr. jun. jul. aug. sep. sept. oct. nov. dec. a.m. p.m. u.s.".split()
)
_NUMBERED = re.compile("v?\\d+(?:[.,]\\d+)*\\.")
_LEAD_IN = re.compile(
    "^(?:(?:so|and|also|then|ok(?:ay)?|right|now|one (?:last |more )?thing|last(?:ly)?|finally|question|decision(?: needed| for you)?|open question|next)\\s*[,:;\\-–—]?\\s+)+",
    re.IGNORECASE,
)
_ADDRESSED = re.compile(
    "^(?:(?:should|shall|can|could|may|would|will|do|did|must) (?:i|we|you)\\b|(?:do|would) you\\b|want me\\b|want us\\b|wanna\\b|(?:are|were) you\\b|have you\\b|(?:is|are) (?:that|this|it|these|those|both|either) (?:ok|okay|fine|alright|acceptable|right|correct|what you)\\b|(?:ok|okay|good|fine|alright|happy) (?:to|with|if|for me)\\b|any (?:objections?|concerns?|preference|thoughts|questions)\\b|which (?:one|option|first|do|should|would|is|of)\\b|(?:what|how|when|where|who) (?:do|would|should|shall|will) (?:you|i|we)\\b|(?:what|which|how|where|when) (?:\\w+ ){0,3}should\\b|how (?:about|do you)\\b|what about\\b)",
    re.IGNORECASE,
)
_STRONG = re.compile(
    "\\b(?:(?:should|shall|may|can) (?:i|we)\\b(?! have\\b)|(?:do|would) you (?:want|like|prefer|approve)\\b|want me to\\b|did you (?:approve|mean|want)\\b)",
    re.IGNORECASE,
)
_SELF_ANSWER = re.compile(
    "^(?:(?:yes|no|nope|not really|probably|maybe|perhaps|because|sure|of course|definitely|absolutely|in short|the answer|turns out)\\b|(?:you|we|i|it|they|that|this|there) (?:do|does|did|is|are|was|were|should|shall|can|could|will|would|must|need|have|has|don't|doesn't|didn't|isn't|aren't|wasn't|weren't|shouldn't|can't|cannot|couldn't|won't|wouldn't|mustn't|needn't|haven't|hasn't)(?: not)?(?:\\s*[.!,;:—–-]|\\s+(?:because|since|as)\\b))",
    re.IGNORECASE,
)
_REPORTED = re.compile(
    "\\b(?:you|owner|they|he|she|codex|claude|the worker|the reviewer|it|someone) (?:asked|asks|wondered|wanted to know|wants to know)\\b",
    re.IGNORECASE,
)
_DECISION_INTRO = re.compile(
    "\\b(?:decisions?|questions?|choices?)\\b[^.?!]*\\b(?:for you|from you|you need to make|waiting on you|need(?:s|ed)? your)\\b[^.?!]*:$",
    re.IGNORECASE,
)
_YOU = re.compile("\\b(?:you|your|you'd|you'll|you're|yours)\\b", re.IGNORECASE)
_SHORT_GO = re.compile(
    "^(?:proceed|push|land|merge|ship|continue|go ahead|approve|apply|commit|release|deploy|start|launch|dispatch|close|retry|revert|delete|send)\\b",
    re.IGNORECASE,
)
_SHORT_GO_WORDS = 10
_BARE_CHOICE = re.compile(
    "^[\\w#./-]+(?: [\\w#./-]+){0,3},? or [\\w#./-]+(?: [\\w#./-]+){0,3}$", re.IGNORECASE
)
_REQUEST = re.compile(
    "^(?:please\\s+)?(?:(?:decide|choose|pick) (?:between|which|whether|one|an? |option|from)|(?:let me know|tell me|say) (?:which|whether|what (?:you|to)|how (?:you|to)|when (?:you|to)|if (?:you want me to|i should|we should|it'?s ok|that'?s ok|you'?re ok|you approve))|confirm (?:whether|which|if|that you|and i'll|to proceed|before)|approve (?:the|this|it|landing|pushing|merging)|give me (?:the |a |your )?(?:go[- ]ahead|ok|okay|green light|answer|decision|call)|your (?:call|decision|pick|choice)\\s*(?:[:.!]|$)|(?:over|up) to you\\b)",
    re.IGNORECASE,
)
_WAITING = re.compile(
    "^(?!.*\\b(?:no|nothing|none|not|never|\\w+n't)\\b).*\\b(?:waiting (?:on|for)|i'll wait for|i'll hold (?:for|until)|holding for|needs?|awaiting|awaits?) (?:your|the owner's) (?:decision|answer|call|go[- ]ahead|ok|okay|approval|authorization|choice|pick|confirmation|say[- ]so|reply|(?:three \\S+ )?commands?)\\b",
    re.IGNORECASE,
)
_OWNER_ACTION = re.compile(
    "^(?:please )?(?:run (?:these|the (?:approved |following )?commands?)\\b|tell me when (?:the |your )?(?:run|command|test)\\b|press enter in (?:its|the|your) tab\\b|push .{0,200}\\bto origin\\b|(?:the )?grants (?:are )?(?:still )?unapproved\\b)",
    re.IGNORECASE,
)
_ANNOUNCEMENT = re.compile(
    "(?:^|[,;:]\\s*|\\b(?:and|then)\\s+)(?:next[, ]+)?(?:i(?:'|’)ll\\s+(?!(?:leave|stop|stand by|keep|park|wait|hold)\\b)\\w+|i(?:'|’)m\\s+(?!(?:leaving|stopping|standing by|keeping|parking|waiting|holding)\\b)\\w+ing\\b|i am\\s+(?!(?:leaving|stopping|standing by|keeping|parking|waiting|holding)\\b)\\w+ing\\b|next i\\s+(?!(?:leave|stop|stand by|keep|park|wait|hold)\\b)\\w+)",
    re.IGNORECASE,
)
_DEPENDENCY = re.compile(
    "\\b(?:when|once|after|until|if|as soon as|whenever|the moment)\\s+\\S|\\bon (?:your )?(?:go|approval|return|word)\\b|^(?:say|give me) (?:the )?go and\\b",
    re.IGNORECASE,
)
_COMMAND_REFERENCE = re.compile(
    "\\bcommands?\\b[^.!?]{0,160}\\b(?:above|in my last message|from my last message)\\b|\\b(?:above|in my last message|from my last message)\\b[^.!?]{0,160}\\bcommands?\\b",
    re.IGNORECASE,
)
_TRAILER = re.compile(
    "^(?:i(?:'d)? recommend|my recommendation|recommendation|i suggest|i lean|my vote|default|if (?:yes|so|not|no|you)|otherwise|either (?:way|is fine)|i'll (?:wait|hold)|i won't)\\b",
    re.IGNORECASE,
)
_LISTS = ("bullet_list_open", "ordered_list_open")
_CODE_ELEMENTS = frozenset({"code", "pre", "kbd", "samp", "tt"})


@dataclass(frozen=True)
class _Sentence:
    detect: str
    compare: str


@dataclass
class _Unit:
    kind: str
    items: list[list[_Sentence]] = field(default_factory=list[list[_Sentence]])


_MARK = "\ue000"


def _inline_children(text: str) -> list[Token]:
    from markdown_it import MarkdownIt

    tokens = MarkdownIt("commonmark").parseInline(text)
    return list(tokens[0].children or []) if tokens else []


class _CodeDepth(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.broken = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _CODE_ELEMENTS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        return

    def handle_endtag(self, tag: str) -> None:
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag) :]

    def take(self, html: str) -> None:
        if self.broken:
            return
        try:
            self.feed(html)
        except Exception:
            self.broken = True

    @property
    def open(self) -> bool:
        return self.broken or bool(self.stack)


def _walk(children: list[Token], code: _CodeDepth | None = None) -> tuple[str, str]:
    code = code or _CodeDepth()
    detect: list[str] = []
    compare: list[str] = []
    for child in children:
        if child.type == "html_inline":
            code.take(child.content)
            continue
        if child.type in ("softbreak", "hardbreak"):
            text = " "
        elif child.type in ("text", "code_inline"):
            text = child.content.replace("\n", " ")
        else:
            continue
        compare.append(text)
        detect.append(_MARK * len(text) if code.open or child.type == "code_inline" else text)
    masked = "".join(detect)
    for pattern in (_URL, _QUOTED):
        masked = pattern.sub(lambda m: _MARK * len(m.group(0)), masked)
    return (masked, "".join(compare))


def _boundaries(detect: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    since = 0
    for match in _SENTENCE_END.finditer(detect):
        run = match.group("run")
        if "?" not in run and "!" not in run:
            if len(run) > 1:
                continue
            word = detect[: match.start("run")].rsplit(None, 1)[-1:] or [""]
            token = (word[0] + run).lower().lstrip("([\"'“‘")
            sentence = detect[since : match.end("run")].strip().lower()
            if token in _ABBREVIATIONS or (_NUMBERED.fullmatch(token) and sentence == token):
                continue
        out.append((match.start("space"), match.end("space")))
        since = match.end("space")
    return out


def _split(detect: str, compare: str) -> list[_Sentence]:
    out: list[_Sentence] = []
    cuts = [0, *(c for start, end in _boundaries(detect) for c in (start, end)), len(detect)]
    for a, b in zip(cuts[::2], cuts[1::2], strict=True):
        piece = detect[a:b]
        lead = len(piece) - len(piece.lstrip())
        trail = len(piece.rstrip())
        if trail > lead:
            out.append(_Sentence(piece[lead:trail], compare[a + lead : a + trail]))
    return out


def _units(text: str) -> list[_Unit]:
    from markdown_it import MarkdownIt

    units: list[_Unit] = []
    stack: list[str] = []
    code = _CodeDepth()
    for token in MarkdownIt("commonmark").parse(text):
        if token.type == "html_block":
            code.take(token.content)
        if token.nesting == 1:
            stack.append(token.type)
            quoted = "blockquote_open" in stack
            if len(stack) == 1 and token.type in _LISTS:
                units.append(_Unit("list"))
            elif len(stack) == 1 and token.type == "paragraph_open":
                units.append(_Unit("p", [[]]))
            elif token.type == "list_item_open" and (not quoted) and units and (stack[0] in _LISTS):
                units[-1].items.append([])
        elif token.nesting == -1:
            if stack:
                stack.pop()
        elif token.type == "inline":
            detect, compare = _walk(list(token.children or []), code)
            if not stack or not units or {"blockquote_open", "heading_open"} & set(stack):
                continue
            top = stack[0]
            if top == "paragraph_open" and units[-1].kind == "p" or (top in _LISTS and units[-1].items):
                units[-1].items[-1].extend(_split(detect, compare))
    return [u for u in units if any(u.items)]


def _clean(sentence: str) -> str:
    s = _LIST_ITEM.sub("", sentence)
    s = _EMPHASIS.sub("", s).strip()
    s = re.sub("^#+\\s*", "", s)
    return _LEAD_IN.sub("", s).strip().rstrip(")]").strip()


def _question_body(sentence: str) -> str:
    s = _clean(sentence).rstrip("!")
    if not s.endswith("?") or s.startswith("|") or _REPORTED.search(s):
        return ""
    return s.rstrip("?!").strip()


def _is_question(sentence: str) -> bool:
    body = _question_body(sentence)
    if not body:
        return False
    tail = _LEAD_IN.sub("", body.rsplit(": ", 1)[-1]).strip()
    if any((_ADDRESSED.match(b) for b in (body, tail))) or _YOU.search(body) or _STRONG.search(body):
        return True
    words = tail.split()
    if words and len(words) <= _SHORT_GO_WORDS and _SHORT_GO.match(tail):
        return True
    return bool(_BARE_CHOICE.match(tail))


def _is_strong(sentence: str) -> bool:
    body = _question_body(sentence)
    tail = _LEAD_IN.sub("", body.rsplit(": ", 1)[-1]).strip()
    return bool(body) and bool(_STRONG.search(body) or _ADDRESSED.match(tail))


def _is_request(sentence: str) -> bool:
    s = _clean(sentence)
    if s.endswith("?") or s.startswith("|"):
        return False
    return bool(_REQUEST.match(s) or _WAITING.search(s) or _OWNER_ACTION.match(s))


def _prose_asks(sentences: list[_Sentence]) -> list[_Sentence]:
    views = [s.detect for s in sentences]
    asks: list[_Sentence] = []
    for i, view in enumerate(views):
        if not _is_question(view):
            continue
        rest = views[i + 1 :]
        if all((_TRAILER.match(_clean(r)) or _is_question(r) for r in rest)) or (
            _is_strong(view) and (not _SELF_ANSWER.match(_clean(rest[0])))
        ):
            asks.append(sentences[i])
    if not asks and views and _is_request(views[-1]):
        asks.append(sentences[-1])
    return asks


def _asks(text: str) -> list[_Sentence]:
    try:
        units = _units(text)
    except Exception:
        return []
    if not units:
        return []
    last = units[-1]
    if last.kind == "p":
        return _prose_asks(last.items[0])
    asks = [a for item in last.items for a in _prose_asks(item)]
    intro = units[-2].items[0] if len(units) > 1 and units[-2].kind == "p" else []
    if intro:
        lead = _clean(intro[-1].detect)
        if _is_question(intro[-1].detect) or (
            _DECISION_INTRO.search(lead) and (not lead.lower().startswith("if "))
        ):
            asks.insert(0, intro[-1])
    return asks


def asks_in(text: str) -> list[str]:
    return [a.compare for a in _asks(text)]


def ask_in(text: str) -> str:
    asks = asks_in(text)
    return asks[-1] if asks else ""


def _announces(sentence: str) -> bool:
    actions = list(_ANNOUNCEMENT.finditer(sentence))
    for i, action in enumerate(actions):
        start = action.start() if i else 0
        end = actions[i + 1].start() if i + 1 < len(actions) else len(sentence)
        if not _DEPENDENCY.search(sentence[start:end]):
            return True
    return False


def ending_issues(text: str) -> tuple[bool, bool]:
    from markdown_it import MarkdownIt

    units = _units(text)
    sentences = [s for unit in units for item in unit.items for s in item]
    closing = units[-1].items[0] if units and units[-1].kind == "p" else []
    question = any((s.detect.rstrip("!").endswith("?") for s in closing)) or bool(_asks(text))
    announcement = not question and any((_announces(s.detect.strip()) for s in closing))
    reference = any((_COMMAND_REFERENCE.search(s.detect) for s in sentences))
    depth = 0
    fenced = False
    for token in MarkdownIt("commonmark").parse(text):
        if token.type == "blockquote_open":
            depth += 1
        elif token.type == "blockquote_close":
            depth -= 1
        elif token.type == "fence" and (not depth) and token.content.strip():
            fenced = True
    return (announcement, reference and (not fenced))


_FRAME = re.compile(
    "^(?:(?:do you want|would you like|want) (?:me|us) to|should (?:i|we)|shall (?:i|we))\\s+", re.IGNORECASE
)


def _normal(compare: str) -> str:
    joined = " ".join(compare.lower().split()).rstrip(" ?.!:;,")
    return _FRAME.sub("", joined).strip()


def comparable(text: str) -> str:
    return _normal(_walk(_inline_children(text))[1])


def _matches(registered: str, key: str) -> bool:
    try:
        return bool(key) and comparable(registered) == key
    except Exception:
        return False


def covers(registered: str, asked: str) -> bool:
    try:
        return _matches(registered, comparable(asked))
    except Exception:
        return False


def _native(payload: dict[str, Any]) -> str:
    return as_str(payload.get("turn_id")) or as_str(payload.get("prompt_id"))


def _state(record: dict[str, Any]) -> dict[str, Any]:
    return dict(as_dict(record.get(STATE)))


def _record(root: Path, session_id: str) -> dict[str, Any] | None:
    record = sessions.get(root, session_id)
    return record if isinstance(record, dict) else None


def _update(root: Path, session_id: str, change: Callable[[dict[str, Any]], dict[str, Any]]) -> None:

    def guarded(record: dict[str, Any]) -> dict[str, Any]:
        return change(record) if isinstance(record, dict) else record

    sessions.update_record(root, session_id, guarded)


def _worker(payload: dict[str, Any], record: dict[str, Any]) -> bool:
    return bool(
        payload.get("agent_id")
        or payload.get("agent_type")
        or record.get("dispatch_id")
        or os.environ.get("AGENT_DISPATCH_ID")
    )


def register(
    root: Path, session_id: str, route: str, text: str | None, payload: dict[str, Any] | None = None
) -> None:
    native = _native(payload or {})

    def change(record: dict[str, Any]) -> dict[str, Any]:
        state = _state(record)
        count = as_int(state.get("turn")) or 0
        entry = {"turn": count, "native": native or as_str(state.get("native")), "route": route}
        if text and text.strip():
            entry["text"] = text[:REGISTERED_CHARS]
        state["asked"] = [*dict_list(state.get("asked")), entry][-KEEP:]
        return {**record, STATE: state}

    try:
        if _record(root, session_id) is not None:
            _update(root, session_id, change)
    except Exception:
        return


def _this_turn(entry: dict[str, Any], state: dict[str, Any], count: int) -> bool:
    if as_int(entry.get("turn")) != count:
        return False
    theirs, ours = (as_str(entry.get("native")), as_str(state.get("native")))
    return not (theirs and ours and (theirs != ours))


def _covered(record: dict[str, Any], state: dict[str, Any], key: str, count: int) -> bool:
    for entry in dict_list(state.get("asked")):
        text = as_str(entry.get("text"))
        if text and _this_turn(entry, state, count) and _matches(text, key):
            return True
    summary = as_str(record.get("owner_question"))
    return bool(record.get("owner_question_pending")) and _matches(summary, key)


_UNREGISTERED = "Unregistered:\n"


def logged_reason(reason: str) -> str:
    _, _, listed = reason.partition(_UNREGISTERED)
    count = len([line for line in listed.splitlines() if line.startswith("- ")])
    return f"ask-guard: {count} uncovered asks"


def _reason(codex: bool, asks: list[str]) -> str:
    listed = _UNREGISTERED + "\n".join((f"- {ask}" for ask in asks))
    if codex:
        return f"ask-guard: your reply ends with questions for the owner that no owner_question was called for. Call owner_question now with `summary` set to each question, so the tab shows the hand and the question; do not repeat them in text. {listed}"
    return f"ask-guard: your reply ends with questions for the owner in plain text, which raise no hand. Ask them now through AskUserQuestion, one decision per question, with the options and your recommendation; do not repeat them in text. {listed}"


def _record_fire(root: Path, session_id: str, record: dict[str, Any], codex: bool, after_block: bool) -> None:
    from ..telemetry import recorder

    feature = as_dict(record.get("feature")).get("id")
    try:
        recorder.record(
            recorder.main_of(root),
            "workflow.ask_guard",
            session=session_id,
            feature=f"{feature:03d}" if type(feature) is int else None,
            data={"client": "codex" if codex else "claude", "after_block": after_block},
        )
    except Exception:
        return


def ask_guard(root: Path, payload: dict[str, Any]) -> str:
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return ""
    record = _record(root, session_id)
    if record is None or _worker(payload, record):
        return ""
    event = str(payload.get("hook_event_name") or "")
    if event == "UserPromptSubmit":
        _new_turn(root, session_id, payload)
        return ""
    from .attention import question_texts, sync_question_tool

    if event == "PreToolUse" and sync_question_tool(str(payload.get("tool_name") or "")):
        for text in question_texts(payload):
            register(root, session_id, "sync", text, payload)
        return ""
    if event != "Stop":
        return ""
    return _stop(root, session_id, payload)


def _new_turn(root: Path, session_id: str, payload: dict[str, Any]) -> None:
    native = _native(payload)
    from .attention import machine_prompt

    def change(record: dict[str, Any]) -> dict[str, Any]:
        state = _state(record)
        state["turn"] = (as_int(state.get("turn")) or 0) + 1
        state["native"] = native
        state["machine_prompt"] = machine_prompt(as_str(payload.get("prompt")))
        state.pop("pending_turn", None)
        return {**record, STATE: state, "feature_peer_delivered": False}

    _update(root, session_id, change)


def _stop(root: Path, session_id: str, payload: dict[str, Any]) -> str:
    text = as_str(payload.get("last_assistant_message"))
    asks = [(a.compare, _normal(a.compare)) for a in _asks(text)]
    try:
        announcement, commands = ending_issues(text)
    except Exception:
        announcement, commands = (False, False)
    blocked = bool(payload.get("_stop_blocked"))
    fired: dict[str, Any] = {}

    def change(record: dict[str, Any]) -> dict[str, Any]:
        state = _state(record)
        count = as_int(state.get("turn")) or 0
        if blocked:
            if "pending_turn" in state and as_int(state.get("pending_turn")) == count:
                return record
            return {**record, STATE: {**state, "pending_turn": count}}
        uncovered = (
            [text for text, key in asks if not _covered(record, state, key, count)]
            if state.get("fired_turn") != count
            else []
        )
        pending_question = record.get("owner_question_pending") and any(
            (_this_turn(entry, state, count) for entry in dict_list(state.get("asked")))
        )
        narrate = announcement and (not pending_question) and (state.get("announcement_turn") != count)
        missing = commands and state.get("commands_turn") != count
        if not (uncovered or narrate or missing):
            return record
        fired.update(
            record=record,
            after_block=as_int(state.get("pending_turn")) == count,
            asks=uncovered,
            announcement=narrate,
            commands=missing,
        )
        state.pop("pending_turn", None)
        for issue, key in (
            (uncovered, "fired_turn"),
            (narrate, "announcement_turn"),
            (missing, "commands_turn"),
        ):
            if issue:
                state[key] = count
        return {**record, STATE: state}

    _update(root, session_id, change)
    if not fired:
        return ""
    record = cast("dict[str, Any]", fired["record"])
    codex = "turn_id" in payload or record.get("provider") == "codex"
    _record_fire(root, session_id, record, codex, bool(fired["after_block"]))
    reasons = [_reason(codex, cast("list[str]", fired["asks"]))] if fired["asks"] else []
    if fired["announcement"]:
        reasons.append(
            "ask-guard: Your reply announced work. If that work can proceed now without the owner or another agent, do it now. If it waits on the owner, a worker or a peer, don't start it: end your turn with one line naming what it waits on."
        )
    if fired["commands"]:
        reasons.append(
            "ask-guard: put the complete commands in this reply's final text, one fenced block each, with resolved operands and no placeholders."
        )
    return json.dumps({"decision": "block", "reason": "\n\n".join(reasons)})
