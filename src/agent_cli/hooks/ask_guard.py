"""Prevent a manager's closing ask from silently appearing idle. Inspect final-paragraph asks and
block Stop once per logical turn unless each matches a registered question exactly after
normalization. Sync registrations keep question text, manual owner_question keeps its summary, and
async registrations have no text to match. Pending manual questions can carry across turns.

Persist one turn counter on UserPromptSubmit. Compaction, continuations, changed client ids and
stop_hook_active do not create a turn. An earlier _stop_blocked preserves the check for the next
Stop without spending its marker. Update check and marker under one session lock.

Fail open on missing or malformed records; never apply to workers or subagents. Keep visible manual
question text in the reply while owner_question supplies the attention state.

Separate once-per-turn checks cover ungated closing work announcements and missing command
references. Dependency gates, non-work endings and an owner question registered this turn exempt
announcements; background work and peer waits alone do not. Prefer missing an ambiguous ask over
repeatedly blocking rhetorical prose.
"""

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
KEEP = 8  # registrations kept on the record, across turns
REGISTERED_CHARS = 4000  # a registered question's raw text kept; a longer one compares truncated

# ------------------------------------------------------------------ the detector

_URL = re.compile(r"(?:https?://|www\.)\S+")
# Quoted speech: a question inside quotes is reported, not asked. It is matched over a whole
# inline block, whose line breaks are spaces by then, so a quotation wrapped over lines is one
# span. The cap keeps two stray marks far apart from masking the prose between them.
_QUOTED = re.compile(r"\"[^\"]{1,1000}\"|“[^”]{1,1000}”|«[^»]{1,1000}»")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+•]|\d{1,2}[.)]|\(?[a-zA-Z]\))\s+")
_EMPHASIS = re.compile(r"[*_~]+")
# A sentence end: a run of `.?!`, any closing brackets or quote marks, then whitespace
# (`_boundaries` decides which runs of dots end one).
_SENTENCE_END = re.compile(r"(?P<run>[.?!]+)[)\]\"'”’»]*(?P<space>\s+)")
# Words a dot does not end a sentence after.
_ABBREVIATIONS = frozenset(
    "e.g. i.e. etc. vs. cf. approx. incl. esp. resp. viz. al. mr. mrs. ms. dr. st. no. nos. fig. "
    "ref. sec. ch. vol. p. pp. ed. est. min. max. jan. feb. mar. apr. jun. jul. aug. sep. sept. "
    "oct. nov. dec. a.m. p.m. u.s.".split()
)
# A number or decimal closed by the dot: "3." "1.2." "v1.2."
_NUMBERED = re.compile(r"v?\d+(?:[.,]\d+)*\.")
_LEAD_IN = re.compile(
    r"^(?:(?:so|and|also|then|ok(?:ay)?|right|now|one (?:last |more )?thing|last(?:ly)?|finally|question"
    r"|decision(?: needed| for you)?|open question|next)\s*[,:;\-–—]?\s+)+",
    re.IGNORECASE,
)

# A question addressed to the owner. Anything else ending in "?" is left alone (rhetorical, a
# note to self, a question about a tool): misses are cheaper than false fires.
_ADDRESSED = re.compile(
    r"^(?:(?:should|shall|can|could|may|would|will|do|did|must) (?:i|we|you)\b"
    r"|(?:do|would) you\b|want me\b|want us\b|wanna\b|(?:are|were) you\b|have you\b"
    r"|(?:is|are) (?:that|this|it|these|those|both|either) (?:ok|okay|fine|alright|acceptable|right|correct|what you)\b"
    r"|(?:ok|okay|good|fine|alright|happy) (?:to|with|if|for me)\b"
    r"|any (?:objections?|concerns?|preference|thoughts|questions)\b"
    r"|which (?:one|option|first|do|should|would|is|of)\b"
    r"|(?:what|how|when|where|who) (?:do|would|should|shall|will) (?:you|i|we)\b"
    r"|(?:what|which|how|where|when) (?:\w+ ){0,3}should\b"
    r"|how (?:about|do you)\b|what about\b"
    r")",
    re.IGNORECASE,
)
# The same address anywhere in the question ("..., or should I send a worker?"). These questions
# end the paragraph's ask even when an explanation follows them, unless an answer does.
_STRONG = re.compile(
    r"\b(?:(?:should|shall|may|can) (?:i|we)\b(?! have\b)|(?:do|would) you (?:want|like|prefer|approve)\b"
    r"|want me to\b|did you (?:approve|mean|want)\b)",
    re.IGNORECASE,
)
# A sentence that answers the question before it: the question was rhetorical.
# "Yes, ...", "You don't; ...", "We should, because ...", "It doesn't." The subject and auxiliary
# must be followed by punctuation or a reason, so "It is small enough to ..." is explanation.
_SELF_ANSWER = re.compile(
    r"^(?:(?:yes|no|nope|not really|probably|maybe|perhaps|because|sure|of course|definitely|absolutely"
    r"|in short|the answer|turns out)\b"
    r"|(?:you|we|i|it|they|that|this|there) (?:do|does|did|is|are|was|were|should|shall|can|could|will"
    r"|would|must|need|have|has|don't|doesn't|didn't|isn't|aren't|wasn't|weren't|shouldn't|can't|cannot"
    r"|couldn't|won't|wouldn't|mustn't|needn't|haven't|hasn't)(?: not)?(?:\s*[.!,;:—–-]|\s+(?:because|since|as)\b))",
    re.IGNORECASE,
)
# Reported speech: someone else's question, not this reply's.
_REPORTED = re.compile(
    r"\b(?:you|owner|they|he|she|codex|claude|the worker|the reviewer|it|someone)"
    r" (?:asked|asks|wondered|wanted to know|wants to know)\b",
    re.IGNORECASE,
)
# A list introduced as decisions put to the owner.
_DECISION_INTRO = re.compile(
    r"\b(?:decisions?|questions?|choices?)\b[^.?!]*\b(?:for you|from you|you need to make|waiting on you|need(?:s|ed)? your)\b"
    r"[^.?!]*:$",
    re.IGNORECASE,
)
_YOU = re.compile(r"\b(?:you|your|you'd|you'll|you're|yours)\b", re.IGNORECASE)
# A bare go/no-go question: "Push?", "Land 045 now?", "Proceed with the merge?"
_SHORT_GO = re.compile(
    r"^(?:proceed|push|land|merge|ship|continue|go ahead|approve|apply|commit|release|deploy|start"
    r"|launch|dispatch|close|retry|revert|delete|send)\b",
    re.IGNORECASE,
)
_SHORT_GO_WORDS = 10
# "A or B?" with no verb: a choice put to the reader.
_BARE_CHOICE = re.compile(r"^[\w#./-]+(?: [\w#./-]+){0,3},? or [\w#./-]+(?: [\w#./-]+){0,3}$", re.IGNORECASE)

# A decision request with no question mark.
_REQUEST = re.compile(
    r"^(?:please\s+)?(?:"
    r"(?:decide|choose|pick) (?:between|which|whether|one|an? |option|from)"
    r"|(?:let me know|tell me|say) (?:which|whether|what (?:you|to)|how (?:you|to)|when (?:you|to)"
    r"|if (?:you want me to|i should|we should|it'?s ok|that'?s ok|you'?re ok|you approve))"
    r"|confirm (?:whether|which|if|that you|and i'll|to proceed|before)"
    r"|approve (?:the|this|it|landing|pushing|merging)"
    r"|give me (?:the |a |your )?(?:go[- ]ahead|ok|okay|green light|answer|decision|call)"
    r"|your (?:call|decision|pick|choice)\s*(?:[:.!]|$)"
    r"|(?:over|up) to you\b"
    r")",
    re.IGNORECASE,
)
_WAITING = re.compile(
    r"^(?!.*\b(?:no|nothing|none|not|never|\w+n't)\b).*"
    r"\b(?:waiting (?:on|for)|i'll wait for|i'll hold (?:for|until)|holding for|needs?|awaiting|awaits?)"
    r" (?:your|the owner's) (?:decision|answer|call|go[- ]ahead|ok|okay|approval|authorization|choice|pick|confirmation|say[- ]so|reply|(?:three \S+ )?commands?)\b",
    re.IGNORECASE,
)
# Recognise owner command handoffs, terminal-enter requests and pending grants as concrete forms
# separate from the question detector.
_OWNER_ACTION = re.compile(
    r"^(?:please )?(?:run (?:these|the (?:approved |following )?commands?)\b"
    r"|tell me when (?:the |your )?(?:run|command|test)\b"
    r"|press enter in (?:its|the|your) tab\b"
    r"|push .{0,200}\bto origin\b"
    r"|(?:the )?grants (?:are )?(?:still )?unapproved\b)",
    re.IGNORECASE,
)
_ANNOUNCEMENT = re.compile(
    r"(?:^|[,;:]\s*|\b(?:and|then)\s+)(?:next[, ]+)?(?:i(?:'|’)ll\s+"
    r"(?!(?:leave|stop|stand by|keep|park|wait|hold)\b)\w+"
    r"|i(?:'|’)m\s+(?!(?:leaving|stopping|standing by|keeping|parking|waiting|holding)\b)\w+ing\b"
    r"|i am\s+(?!(?:leaving|stopping|standing by|keeping|parking|waiting|holding)\b)\w+ing\b"
    r"|next i\s+(?!(?:leave|stop|stand by|keep|park|wait|hold)\b)\w+)",
    re.IGNORECASE,
)
_DEPENDENCY = re.compile(
    r"\b(?:when|once|after|until|if|as soon as|whenever|the moment)\s+\S"
    r"|\bon (?:your )?(?:go|approval|return|word)\b"
    r"|^(?:say|give me) (?:the )?go and\b",
    re.IGNORECASE,
)
_COMMAND_REFERENCE = re.compile(
    r"\bcommands?\b[^.!?]{0,160}\b(?:above|in my last message|from my last message)\b"
    r"|\b(?:above|in my last message|from my last message)\b[^.!?]{0,160}\bcommands?\b",
    re.IGNORECASE,
)
# Sentences that may follow the ask in its paragraph without ending it: a recommendation or the
# terms of the answer ("Should I push? If yes, I'll land it.").
_TRAILER = re.compile(
    r"^(?:i(?:'d)? recommend|my recommendation|recommendation|i suggest|i lean|my vote|default"
    r"|if (?:yes|so|not|no|you)|otherwise|either (?:way|is fine)|i'll (?:wait|hold)|i won't)\b",
    re.IGNORECASE,
)


_LISTS = ("bullet_list_open", "ordered_list_open")
# HTML elements whose content is code, not prose.
_CODE_ELEMENTS = frozenset({"code", "pre", "kbd", "samp", "tt"})


@dataclass(frozen=True)
class _Sentence:
    """One sentence, from one token walk (`_walk`): `detect` is what the detector reads, with
    code and quotations masked; `compare` is the real text at the same offsets."""

    detect: str
    compare: str


@dataclass
class _Unit:
    """One top-level block of prose: a paragraph (`items` holds its one sentence list) or a list
    (one sentence list per item, nested items included)."""

    kind: str  # "p" or "list"
    items: list[list[_Sentence]] = field(default_factory=list[list[_Sentence]])


# What `detect` holds in place of each character of code (a code span, an inline code element's
# content), a URL or a quotation, so a question inside one never asks and never ends a sentence.
_MARK = ""


def _inline_children(text: str) -> list[Token]:
    """`text` parsed as CommonMark inline content (markdown-it-py)."""
    from markdown_it import MarkdownIt

    tokens = MarkdownIt("commonmark").parseInline(text)
    return list(tokens[0].children or []) if tokens else []


class _CodeDepth(HTMLParser):
    """Which code elements (`_CODE_ELEMENTS`) are open, read from a real HTML tokenizer (stdlib
    `html.parser`), never from a regex over raw HTML: tag-shaped text in comments, quoted
    attributes (a `>` included), scripts, CDATA and processing instructions is not a tag to it.
    One instance is fed every HTML token of a reply, inline and block, in document order, so an
    element opened in one paragraph stays open until its real close.

    The open elements are a stack. An end tag closes the most recent open element of the same
    name and anything opened above it (an implied close, as HTML does); an end tag with no open
    element of its name (`</kbd>` while only `<code>` is open) closes nothing. If the tokenizer
    raises, everything after counts as code: the guard fails toward not an ask."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.broken = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _CODE_ELEMENTS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        return  # `<code/>` opens nothing

    def handle_endtag(self, tag: str) -> None:
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag) :]

    def take(self, html: str) -> None:
        if self.broken:
            return
        try:
            self.feed(html)
        except Exception:  # noqa: BLE001 — a tokenizer failure fails toward not an ask
            self.broken = True

    @property
    def open(self) -> bool:
        return self.broken or bool(self.stack)


def _walk(children: list[Token], code: _CodeDepth | None = None) -> tuple[str, str]:
    """The one token walk: an inline token's children, in order, as two strings of identical
    length, `detect` and `compare`.

    Segments: prose is each `text` token (emphasis, strong and link wrappers are tokens and drop
    out, so a literal `a**b` is text and stays); code is each `code_inline`'s content and every
    `text` or line break while an HTML `<code>`/`<kbd>`/`<samp>`/`<tt>`/`<pre>` is open. `code`
    is the reply's one tokenizer (`_CodeDepth`), fed each `html_inline` here and each
    `html_block` by `_units`, so an element left open stays code until its close, or to the end.
    A soft or hard line break is a space in both strings: a line break never ends a sentence.
    `compare` is every segment's real text (escapes and entities already decoded by the parser);
    `detect` is the same with code, URLs and quotations (straight, curly or guillemets, entity
    encoded or not, wrapped over lines or not) replaced by `_MARK` character for character."""
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
    return masked, "".join(compare)


def _boundaries(detect: str) -> list[tuple[int, int]]:
    """Where sentences end in `detect`: the whitespace after a run of `.?!` (and any closing
    bracket or quote mark after it), outside masks. A lowercase start never prevents a boundary.

    - A run holding `?` or `!` always ends a sentence.
    - An ellipsis (two or more dots, or `…`, which is not in the run at all) never does: "Should
      I not ... push main?" is one question that keeps its negation.
    - One `.` after a known abbreviation (`_ABBREVIATIONS`) never ends a sentence, whatever
      follows. Whether it does is ambiguous both ways, so the guard takes the safer side: a
      wrong split can drop a negation or an action ("Should I not use e.g. GitHub or GitLab?"
      leaving "GitHub or GitLab?") and so pass a different decision, while a wrong merge usually
      costs one extra nudge. Residual, by design: "The checks took one min. Should I push
      main?" is one merged ask, which a registered "Should I push main?" does not cover, so it
      nudges once. A wrong merge can also miss an ask (documented corpus misses): a short action
      question ("The checks took one min. Push?") loses the sentence start `_SHORT_GO` needs, and
      a status holding reported speech ("You asked about the U.S. Should I push main?") puts the
      later ask under the `_REPORTED` exclusion.
    - One `.` after a number or decimal that is the whole sentence so far is an enumeration
      ("2. Should I launch S1?") and runs on; a number closing prose ("then 2. Which ...") ends
      the sentence.
    - Any other single `.` ends a sentence, whatever case follows."""
    out: list[tuple[int, int]] = []
    since = 0
    for match in _SENTENCE_END.finditer(detect):
        run = match.group("run")
        if "?" not in run and "!" not in run:
            if len(run) > 1:
                continue  # an ellipsis
            word = detect[: match.start("run")].rsplit(None, 1)[-1:] or [""]
            token = (word[0] + run).lower().lstrip("([\"'“‘")
            sentence = detect[since : match.end("run")].strip().lower()
            if token in _ABBREVIATIONS or (_NUMBERED.fullmatch(token) and sentence == token):
                continue
        out.append((match.start("space"), match.end("space")))
        since = match.end("space")
    return out


def _split(detect: str, compare: str) -> list[_Sentence]:
    """One inline block's sentences, cut on `detect` only, at `.?!` outside masks; the same
    offsets slice `compare`. Blocks (paragraphs, list items) are separate calls, so a block
    boundary always ends a sentence. Nothing is parsed again."""
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
    """`text`'s top-level paragraphs and lists, parsed as CommonMark (markdown-it-py). Only the
    inline content of paragraphs and list items counts, and none inside a block quote or a
    heading, at any depth: fenced and indented code, HTML blocks (`<pre>`), headings (in a list
    item too) and quotes (lazy continuation lines included) contribute nothing, so the unit
    before them stays the last."""
    from markdown_it import MarkdownIt

    units: list[_Unit] = []
    stack: list[str] = []
    code = _CodeDepth()  # one tokenizer for every HTML token of the reply, in document order
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
            elif token.type == "list_item_open" and not quoted and units and stack[0] in _LISTS:
                units[-1].items.append([])
        elif token.nesting == -1:
            if stack:
                stack.pop()
        elif token.type == "inline":
            detect, compare = _walk(list(token.children or []), code)
            if not stack or not units or {"blockquote_open", "heading_open"} & set(stack):
                continue  # walked only to carry the code depth
            top = stack[0]
            if (top == "paragraph_open" and units[-1].kind == "p") or (top in _LISTS and units[-1].items):
                units[-1].items[-1].extend(_split(detect, compare))
    return [u for u in units if any(u.items)]


def _clean(sentence: str) -> str:
    s = _LIST_ITEM.sub("", sentence)
    s = _EMPHASIS.sub("", s).strip()
    s = re.sub(r"^#+\s*", "", s)
    return _LEAD_IN.sub("", s).strip().rstrip(")]").strip()


def _question_body(sentence: str) -> str:
    """The question `sentence` asks, without its "?", or "" when it asks none or reports one."""
    s = _clean(sentence).rstrip("!")  # "?!" asks too
    if not s.endswith("?") or s.startswith("|") or _REPORTED.search(s):
        return ""
    return s.rstrip("?!").strip()


def _is_question(sentence: str) -> bool:
    """A question put to the owner. A lead-in before a colon ("One question is open: did you ...?")
    is read past."""
    body = _question_body(sentence)
    if not body:
        return False
    tail = _LEAD_IN.sub("", body.rsplit(": ", 1)[-1]).strip()
    if any(_ADDRESSED.match(b) for b in (body, tail)) or _YOU.search(body) or _STRONG.search(body):
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
    """Every ask in a run of sentences, in order: a question put to the owner that ends the run,
    or that only other such questions, a recommendation or the terms of the answer follow
    ("push main? close it?" is two asks); an explicitly addressed question ("Should I ...?") followed by
    explanation but not an answer; and, when no question asks, a decision
    request that ends the run. Detection reads `detect` only."""
    views = [s.detect for s in sentences]
    asks: list[_Sentence] = []
    for i, view in enumerate(views):
        if not _is_question(view):
            continue
        rest = views[i + 1 :]
        if all(_TRAILER.match(_clean(r)) or _is_question(r) for r in rest) or (
            _is_strong(view) and not _SELF_ANSWER.match(_clean(rest[0]))
        ):
            asks.append(sentences[i])
    if not asks and views and _is_request(views[-1]):
        # After a question, "I'll wait for your reply." is that question's trailer, not a second ask.
        asks.append(sentences[-1])
    return asks


def _asks(text: str) -> list[_Sentence]:
    """Every ask in `text`'s final paragraph or list (`_units`; outside code, block quotes, HTML
    and quoted speech), in order. A final list asks through each item that asks a question, and
    through the sentence that introduced it when that is a question or names the items as
    decisions for the owner. If the Markdown parser fails, nothing asks: the guard fails open."""
    try:
        units = _units(text)
    except Exception:  # noqa: BLE001 — a parser failure never costs the session its turn
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
        # A list of things the owner owes ("Needs you:", "Two steps need you:") is not a question:
        # the obligations surface shows those. Only a list named as decisions or questions asks.
        if _is_question(intro[-1].detect) or (
            _DECISION_INTRO.search(lead) and not lead.lower().startswith("if ")
        ):
            asks.insert(0, intro[-1])
    return asks


def asks_in(text: str) -> list[str]:
    """Every ask in `text`'s final paragraph or list (`_asks`), each as its rendered text."""
    return [a.compare for a in _asks(text)]


def _announces(sentence: str) -> bool:
    """An ungated own action. Each action's gate covers only its clause, not a later action."""
    actions = list(_ANNOUNCEMENT.finditer(sentence))
    for i, action in enumerate(actions):
        start = action.start() if i else 0  # include a leading "When ..., I'll ..."
        end = actions[i + 1].start() if i + 1 < len(actions) else len(sentence)
        if not _DEPENDENCY.search(sentence[start:end]):
            return True
    return False


def ending_issues(text: str) -> tuple[bool, bool]:
    """Closing-paragraph announcement and missing-command reference, outside quotes or code.
    Questions and dependencies exempt announcements. A real fence covers a command reminder;
    backticks around an operand, or a fence quoted from another message, do not."""
    from markdown_it import MarkdownIt

    units = _units(text)
    sentences = [s for unit in units for item in unit.items for s in item]
    closing = units[-1].items[0] if units and units[-1].kind == "p" else []
    question = any(s.detect.rstrip("!").endswith("?") for s in closing) or bool(_asks(text))
    announcement = not question and any(_announces(s.detect.strip()) for s in closing)
    reference = any(_COMMAND_REFERENCE.search(s.detect) for s in sentences)
    depth = 0
    fenced = False
    for token in MarkdownIt("commonmark").parse(text):
        if token.type == "blockquote_open":
            depth += 1
        elif token.type == "blockquote_close":
            depth -= 1
        elif token.type == "fence" and not depth and token.content.strip():
            fenced = True
    return announcement, reference and not fenced


# ------------------------------------------------------------------ coverage

# The asking phrase, dropped before comparing: "want me to push main" asks "should I push main".
_FRAME = re.compile(
    r"^(?:(?:do you want|would you like|want) (?:me|us) to|should (?:i|we)|shall (?:i|we))\s+",
    re.IGNORECASE,
)


def _normal(compare: str) -> str:
    """`compare` text as coverage compares it: lower case, whitespace collapsed, trailing
    `?.!:;,` stripped, and the leading asking phrase ("should I", "want me to") dropped. Nothing
    else is removed: quote marks, negations, direction words, lead-ins and every operand stay."""
    joined = " ".join(compare.lower().split()).rstrip(" ?.!:;,")
    return _FRAME.sub("", joined).strip()


def comparable(text: str) -> str:
    """A registration's raw text (AskUserQuestion, request_user_input, owner_question) as
    coverage compares it: the same token walk an ask's sentence comes from (`_walk`), its
    `compare` string, then `_normal`. An ask's comparison text is its sentence's `compare` slice
    through `_normal`, so identical Markdown on both sides always compares equal."""
    return _normal(_walk(_inline_children(text))[1])


def _matches(registered: str, key: str) -> bool:
    """Match normalized question text exactly. Similarity and containment can conflate different
    decisions or negations, so a partial registration cannot authorize a more specific ask.
    """
    try:
        return bool(key) and comparable(registered) == key
    except Exception:  # noqa: BLE001 — unparseable text covers nothing: the ask fires
        return False


def covers(registered: str, asked: str) -> bool:
    """Whether a registered question covers a question written as `asked`, both raw text."""
    try:
        return _matches(registered, comparable(asked))
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ turn identity and state


def _native(payload: dict[str, Any]) -> str:
    return as_str(payload.get("turn_id")) or as_str(payload.get("prompt_id"))


def _state(record: dict[str, Any]) -> dict[str, Any]:
    return dict(as_dict(record.get(STATE)))


def _record(root: Path, session_id: str) -> dict[str, Any] | None:
    """The session record, or None when there is none or it is not a mapping (`[]`, `null`, a
    number): then the guard does nothing at all, neither blocks nor writes. Unparseable JSON
    raises, and `run_one` fails the member open."""
    record = sessions.get(root, session_id)  # typed a dict, but the file can hold any JSON
    return record if isinstance(record, dict) else None


def _update(root: Path, session_id: str, change: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    """One locked read-modify-write of a record already known to be a mapping. A record that
    stopped being one under the lock is returned unchanged, so nothing is written."""

    def guarded(record: dict[str, Any]) -> dict[str, Any]:
        return change(record) if isinstance(record, dict) else record  # pyright: ignore[reportUnnecessaryIsInstance]

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
    """Note a question raised in this session's current turn (table in the module docstring).
    Best effort: a lost registration costs at most one extra turn."""
    native = _native(payload or {})

    def change(record: dict[str, Any]) -> dict[str, Any]:
        state = _state(record)
        count = as_int(state.get("turn")) or 0
        entry = {"turn": count, "native": native or as_str(state.get("native")), "route": route}
        if text and text.strip():
            entry["text"] = text[:REGISTERED_CHARS]  # raw: `comparable` runs at comparison time
        state["asked"] = [*dict_list(state.get("asked")), entry][-KEEP:]
        return {**record, STATE: state}

    try:
        if _record(root, session_id) is not None:
            _update(root, session_id, change)
    except Exception:  # noqa: BLE001 — a question is asked whether or not this lands
        return


def _this_turn(entry: dict[str, Any], state: dict[str, Any], count: int) -> bool:
    """A registration belongs to the logical turn `count`. The client's id only narrows that: a
    registration carrying a different id from the one the turn's prompt carried is not counted
    (so the ask fires), and a missing id on either side changes nothing."""
    if as_int(entry.get("turn")) != count:
        return False
    theirs, ours = as_str(entry.get("native")), as_str(state.get("native"))
    return not (theirs and ours and theirs != ours)


def _covered(record: dict[str, Any], state: dict[str, Any], key: str, count: int) -> bool:
    """Whether a question registered with its text in this turn covers the ask whose comparison
    text is `key` (`_matches`), or a still-pending `owner_question` from an earlier turn does (a
    wake turn re-asking what the hand already holds). A registration with no text covers
    nothing: the guard cannot tell which decision it was."""
    for entry in dict_list(state.get("asked")):
        text = as_str(entry.get("text"))
        if text and _this_turn(entry, state, count) and _matches(text, key):
            return True
    summary = as_str(record.get("owner_question"))
    return bool(record.get("owner_question_pending")) and _matches(summary, key)


_UNREGISTERED = "Unregistered:\n"


def logged_reason(reason: str) -> str:
    """What `decision_log` keeps of an ask-guard block reason: a fixed line and the number of
    asks, never their text. The reason itself goes only to the model."""
    _, _, listed = reason.partition(_UNREGISTERED)
    count = len([line for line in listed.splitlines() if line.startswith("- ")])
    return f"ask-guard: {count} uncovered asks"


def _reason(codex: bool, asks: list[str]) -> str:
    listed = _UNREGISTERED + "\n".join(f"- {ask}" for ask in asks)
    if codex:
        return (
            "ask-guard: your reply ends with questions for the owner that no owner_question was "
            "called for. Call owner_question now with `summary` set to each question, so the tab "
            f"shows the hand and the question; do not repeat them in text. {listed}"
        )
    return (
        "ask-guard: your reply ends with questions for the owner in plain text, which raise no "
        "hand. Ask them now through AskUserQuestion, one decision per question, with the options "
        f"and your recommendation; do not repeat them in text. {listed}"
    )


def _record_fire(root: Path, session_id: str, record: dict[str, Any], codex: bool, after_block: bool) -> None:
    """`workflow.ask_guard`: a detected raw-text ask (not every raw-text ask: the detector
    misses on purpose). Ids and shape only, never the question."""
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
    except Exception:  # noqa: BLE001 — the block stands whether or not its event lands
        return


# ------------------------------------------------------------------ the hook


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
        # Each question of a batched call is its own decision, so each covers only itself.
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
    """Judge one Stop. The decision and its marker are one locked read-modify-write, so two
    concurrent Stops of a turn cannot both block. The marker is the logical turn's count, never
    the client's id: no change, addition or removal of an id between Stops makes a new turn."""
    text = as_str(payload.get("last_assistant_message"))
    asks = [(a.compare, _normal(a.compare)) for a in _asks(text)]
    try:
        announcement, commands = ending_issues(text)
    except Exception:  # noqa: BLE001 — a parser failure leaves the turn free to end
        announcement, commands = False, False
    blocked = bool(payload.get("_stop_blocked"))
    fired: dict[str, Any] = {}

    def change(record: dict[str, Any]) -> dict[str, Any]:
        state = _state(record)
        count = as_int(state.get("turn")) or 0
        if blocked:
            # The turn goes on anyway: spend nothing, and judge the next Stop of this turn.
            if "pending_turn" in state and as_int(state.get("pending_turn")) == count:
                return record
            return {**record, STATE: {**state, "pending_turn": count}}
        uncovered = (
            [text for text, key in asks if not _covered(record, state, key, count)]
            if state.get("fired_turn") != count
            else []
        )
        pending_question = record.get("owner_question_pending") and any(
            _this_turn(entry, state, count) for entry in dict_list(state.get("asked"))
        )
        narrate = announcement and not pending_question and state.get("announcement_turn") != count
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
            "ask-guard: Your reply announced work. If that work can proceed now without the "
            "owner or another agent, do it now. If it waits on the owner, a worker or a peer, "
            "don't start it: end your turn with one line naming what it waits on."
        )
    if fired["commands"]:
        reasons.append(
            "ask-guard: put the complete commands in this reply's final text, one fenced "
            "block each, with resolved operands and no placeholders."
        )
    return json.dumps({"decision": "block", "reason": "\n\n".join(reasons)})
