import asyncio
import contextlib
import json
import logging
import os
import re
from typing import Any

import websockets
from livekit.agents import FlushSentinel, llm
from livekit.agents.llm import ChatChunk, ChatContext, ChoiceDelta, LLMStream

# Default connection options matching base LLM.chat signature
from livekit.agents.llm.llm import DEFAULT_API_CONNECT_OPTIONS
from websockets.protocol import State

logger = logging.getLogger("hermes-llm")

# How long we wait for the prompt.submit RPC ack before declaring the gateway
# unresponsive. In practice it arrives in <50ms; 30s only catches real hangs.
SUBMIT_ACK_TIMEOUT = 30.0


class _VoiceFlush(FlushSentinel):
    """FlushSentinel with a no-op `.id`/`.delta` surface.

    The SDK routes FlushSentinel from an LLM stream straight into the TTS
    segment pipeline (isinstance check), but its metrics monitor tees the
    same channel and reads `ev.id` on every event — a bare FlushSentinel
    would crash it mid-turn. This subclass keeps isinstance routing while
    satisfying the monitor.
    """

    id: str = ""
    delta: object = None
    usage: object = None


# ---------------------------------------------------------------------------
# Lapis 1 — Voice instructions (plan ui-integration.md §4).
# Stateless: prepended to every prompt.submit, because LiveKit `instructions=`
# NEVER reach Hermes (hermes_llm only forwards the last user message).
# ---------------------------------------------------------------------------
VOICE_INSTRUCTIONS = """\
[VOICE MODE] You are on an interactive realtime voice call with the user (Schnee).
- Personality & Engagement: Be warm, proactive, and engaging. After answering or completing an action, proactively offer the next step, ask a helpful follow-up question, or suggest what to do next.
- Keep replies concise, conversational, and spoken-friendly (2-3 short sentences or a tight 2-3 bullet list max). When listing items, format with clear markdown (e.g. bold the title like **Title**: description or **Title** on each bullet '- **Title**: details'). Always use '-' on new lines.
- End every list response with a natural follow-up question on its own clean paragraph (e.g. "Would you like me to look into one of these?").
- Write numbers, dates, and amounts as standard digits (e.g. 25, 2026, 1.500).
- Delivery cues: start EVERY reply with a bracket cue describing how the first sentence should be delivered (e.g. [warm], [cheerful], [soft], [calm]). Cues are for TTS style and will be stripped automatically from the text display.
- Language policy: ALWAYS reply in English. Switch to Indonesian ONLY when the user explicitly asks for Indonesian (e.g. "pakai bahasa Indonesia", "jawab dalam bahasa Indonesia", "ngomong bahasa Indonesia"). If the user switches back to Indonesian without such a request, keep replying in English.
- Tool Call Limit & Latency Budget: Voice mode requires fast turnaround. Use at most 1-3 tool calls per turn. Never run long iterative multi-step research loops. If deep multi-step exploration or heavy parallel research is needed, delegate it to a background subagent (`delegate_task`) and report the immediate status/findings to the user.
- When you need to look something up, search, or run any tool: FIRST speak one short, natural, and VARIED sentence indicating that you're looking into it (e.g. "Give me a quick moment.", "Checking that now.", "Hmm, let me see...", "I'll pull up the details.", "Looking into it right away."), THEN run the tool. Avoid always starting with 'Let me check...' every single time.
- IMPORTANT: If you need to run MULTIPLE tools in sequence, speak ONLY ONE opening sentence before the FIRST tool. Do NOT speak again between tools — stay silent until all tools complete, then give the final answer. Example: "Looking into that now..." [tool 1 runs silently] [tool 2 runs silently] [tool 3 runs silently] "Here's what I found..."
- If asked for code or technical details: explain briefly in words; never output long unformatted code blocks."""

# ---------------------------------------------------------------------------
# Lapis 3 — Smart Filler Engine (v6).
#
# v5 removed all voice fillers. v6 adds them back, but intelligently:
# - Fast tools (< 0.8s) → NO filler at all, straight to final answer
# - Slow tools (> 0.8s) → ONE opening filler via session.say(), not LLM text
# - Multi-tool sequences → filler only on the FIRST slow tool; subsequent
#   tools get a dwell filler ("Still looking...") only if the total silence
#   exceeds 4s since the last spoken text
#
# Key insight from LiveKit docs: use session.say() for fillers, NOT
# LLM-generated text. LLM emits the opening sentence and the tool call in
# the same burst, so the TTS never gets a head start. session.say()
# bypasses the LLM entirely and speaks immediately.
#
# The filler is injected from the bridge side (hermes_llm) because that's
# where we see the tool.generating / tool.complete events. We don't have
# direct access to session.say() from here, so we emit the filler as a
# normal ChatChunk + FlushSentinel — the SDK routes it to TTS immediately.
# ---------------------------------------------------------------------------

# Filler pools — rotated randomly so repeated calls don't sound canned.
# Opening fillers: spoken when a slow tool starts (> 0.3s).
# Written to sound natural and conversational, not robotic.
# Includes disfluency (hmm, uh, well) for human-like quality.
_OPENING_FILLERS = [
    "[soft] Let me check that for you.",
    "[warm] Give me just a moment.",
    "[gentle] One sec, looking into it.",
    "[soft] Hmm, let me see...",
    "[warm] Ah, checking now...",
    "[gentle] Just a moment, please.",
    "[soft] Well, let me take a look.",
    "[warm] Okay, one second...",
]

# Dwell fillers: spoken when total silence exceeds 4s during multi-tool.
# These acknowledge the wait without repeating the opening filler.
# More casual and varied to sound like genuine thinking.
_DWELL_FILLERS = [
    "[soft] Hmm, still looking...",
    "[warm] Almost there...",
    "[gentle] Just a bit longer...",
    "[soft] One more moment...",
    "[warm] Still working on it...",
    "[soft] Bear with me...",
    "[gentle] Taking a little longer than expected...",
    "[warm] Hmm, this is quite thorough...",
]

# Timing thresholds
_TOOL_FAST_THRESHOLD = 0.3  # seconds — tools faster than this get NO filler
_DWELL_THRESHOLD = 4.0  # seconds of silence before dwell filler kicks in
_EARLY_ACK_THRESHOLD = 2.0  # seconds — if LLM hasn't emitted first token/sentence in 2s, speak an ack filler


class _FillerEngine:
    """Manages filler injection for one turn.

    Tracks tool timing and decides when to inject opening/dwell fillers.
    All state is per-turn; a new engine is created for each _run_turn call.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._t_turn_start = loop.time()
        self._t_last_spoken: float | None = None  # last time ANY text was sent to TTS
        self._t_first_tool_start: float | None = None
        self._opening_filler_sent = False
        self._dwell_filler_sent = False
        self._filler_task: asyncio.Task | None = None
        self._tool_count = 0
        self._active_tool_count = 0  # tools currently running
        self._last_tool_name = ""
        self._tool_active = False  # True between tool.generating and tool.complete

    def record_spoken(self) -> None:
        """Call whenever any text is sent to TTS (user-facing or filler)."""
        self._t_last_spoken = self._loop.time()

    @property
    def tool_count(self) -> int:
        """Number of tools started this turn."""
        return self._tool_count

    @property
    def tool_active(self) -> bool:
        """True while a tool is actively running."""
        return self._tool_active

    @property
    def opening_filler_sent(self) -> bool:
        """True if the opening filler has been sent."""
        return self._opening_filler_sent

    def reset_dwell(self) -> None:
        """Reset dwell filler state so it can fire again for the next tool."""
        self._dwell_filler_sent = False

    def record_tool_start(self, tool_name: str) -> bool:
        """Record a tool starting. Returns True if this is the FIRST tool."""
        self._tool_count += 1
        self._active_tool_count += 1
        self._tool_active = True
        self._last_tool_name = tool_name
        if self._t_first_tool_start is None:
            self._t_first_tool_start = self._loop.time()
            return True
        return False

    def record_tool_end(self) -> None:
        """Record a tool completing."""
        self._active_tool_count = max(0, self._active_tool_count - 1)
        if self._active_tool_count == 0:
            self._tool_active = False

    @property
    def has_active_tools(self) -> bool:
        """True if any tool is currently running."""
        return self._active_tool_count > 0

    def cancel_pending(self) -> None:
        """Cancel any pending filler task."""
        if self._filler_task is not None and not self._filler_task.done():
            self._filler_task.cancel()
            self._filler_task = None

    async def schedule_early_ack(self, send_filler) -> None:
        """Schedule an early acknowledge filler if the LLM takes > 2s to emit anything.

        Fires if no speech or tool has started within _EARLY_ACK_THRESHOLD seconds,
        preventing dead air during LLM API queue / latency spikes.
        """
        self.cancel_pending()

        async def _fire() -> None:
            await asyncio.sleep(_EARLY_ACK_THRESHOLD)
            if not self._opening_filler_sent and self._t_last_spoken is None:
                filler = _OPENING_FILLERS[
                    hash(str(self._t_turn_start) + "early") % len(_OPENING_FILLERS)
                ]
                logger.info(
                    "Filler engine: early ack filler after %.1fs TTFT latency",
                    _EARLY_ACK_THRESHOLD,
                )
                await send_filler(filler)
                self._opening_filler_sent = True
                self.record_spoken()

        self._filler_task = asyncio.create_task(_fire())

    async def schedule_opening(self, send_filler) -> None:
        """Schedule an opening filler if the tool is slow enough.

        send_filler is an async callable that takes a filler string and
        sends it to TTS. This method waits _TOOL_FAST_THRESHOLD seconds;
        if the tool hasn't completed by then, it fires the filler.
        """
        self.cancel_pending()

        async def _fire() -> None:
            await asyncio.sleep(_TOOL_FAST_THRESHOLD)
            if not self._opening_filler_sent:
                filler = _OPENING_FILLERS[
                    hash(str(self._t_turn_start)) % len(_OPENING_FILLERS)
                ]
                logger.info(
                    "Filler engine: opening filler after %.1fs", _TOOL_FAST_THRESHOLD
                )
                await send_filler(filler)
                self._opening_filler_sent = True
                self.record_spoken()

        self._filler_task = asyncio.create_task(_fire())

    async def schedule_dwell(self, send_filler) -> None:
        """Schedule a dwell filler for extended silence during multi-tool.

        Fires if no text has been spoken for _DWELL_THRESHOLD seconds,
        and the turn is still ongoing. Can repeat every _DWELL_THRESHOLD seconds
        if multi-tool execution takes very long.
        """
        self.cancel_pending()

        async def _fire() -> None:
            await asyncio.sleep(_DWELL_THRESHOLD)
            # Check if we've been silent the whole time
            if (
                self._t_last_spoken is not None
                and self._loop.time() - self._t_last_spoken >= _DWELL_THRESHOLD - 0.5
            ):
                filler = _DWELL_FILLERS[
                    hash(str(self._t_turn_start) + str(self._loop.time()) + "dwell")
                    % len(_DWELL_FILLERS)
                ]
                logger.info(
                    "Filler engine: dwell filler after %.1fs silence", _DWELL_THRESHOLD
                )
                await send_filler(filler)
                self._dwell_filler_sent = True
                self.record_spoken()

        self._filler_task = asyncio.create_task(_fire())


# ---------------------------------------------------------------------------
# Lapis 2 — Sentence & Clause splitter + cleaner (TTS safety net).
# ---------------------------------------------------------------------------
# Boundary chars for sentence splitting: only `.`, `!`, `?`, newline.
# Commas and semicolons are NOT boundaries — they are mid-sentence pauses.
# Splitting on commas creates paragraph-like breaks in the client UI and
# unnatural TTS prosody. The TTS tokenizer handles comma pauses internally.
_BOUNDARY_CHARS = ".!?\n"
_MIN_SENTENCE_LEN = 6  # Reduced 12 -> 6 so first short clause ("Tentu,", "Baik,") streams instantly to TTS
_MAX_PENDING_LEN = 250  # Lowered from 400 to force-cut long clauses sooner

_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`([^`\n]*)`")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s+", re.MULTILINE)
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_HR_RE = re.compile(r"^\s{0,3}(?:-{3,}|={3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)
_TABLE_PIPE_RE = re.compile(r"\|")
_EMPHASIS_RE = re.compile(r"[*_~]")
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\u2190-\u21ff"
    "\u2300-\u23ff\u25a0-\u25ff\ufe00-\ufe0f\u200d\U0001f1e6-\U0001f1ff"
    "\u2764\u270c\u270b]"
)
_ZERO_WIDTH_RE = re.compile("[\u200b-\u200f\u2060\ufeff\u00ad]")
_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f]")
_REPEAT_PUNCT_RE = re.compile(r"([!?])\1+")

# Hermes gateway steering scaffolds — internal machinery the core writes into
# its own history when a live turn gets redirected/interrupted mid-flight
# (agent/conversation_loop.py). The model sometimes echoes this scaffolding
# back in its next reply, and replay paths can stream it to us. It must never
# reach TTS or the user's transcript (2026-08-14: observed leaking into the
# chat as "[interruption..]" blocks after redirected turns).
_SCAFFOLD_MARKERS = (
    "this response was interrupted by a user correction",
    "visible response before the interruption",
    "context from the interrupted assistant response",
)
_MOJIBAKE: list[tuple[str, str]] = [
    ("\u00e2\u20ac\u201d", "-"),  # â€" → -
    ("\u00e2\u20ac\u201c", "-"),  # â€" → -
    ("\u00e2\u20ac\u0153", '"'),  # â€œ → "
    ("\u00e2\u20ac\u02dc", "'"),  # â€(lsquo) → '
    ("\u00e2\u20ac\u2122", "'"),  # â€™ → '
    ("\u00e2\u20ac\u00a6", "..."),  # â€¦ → ...
    ("\u00c3\u00a9", "\u00e9"),  # Ã© → é
    ("\u00c3\u00a8", "\u00e8"),  # Ã¨ → è
    ("\u00c3\u00a0", "\u00e0"),  # Ã  → à
    ("\u00e2\u20ac", '"'),  # bare â€ prefix catch-all — must stay last
]


def contains_scaffold(text: str) -> bool:
    """True if the text carries Hermes steering scaffolding (interruption
    markers) rather than real assistant prose."""
    low = text.lower()
    return any(marker in low for marker in _SCAFFOLD_MARKERS)


def clean_voice_text(text: str) -> str:
    """Strip markdown/emoji/URLs/control chars from one chunk of voice text.

    Server-side mirror of the client's cleanVoiceText() (voice-text.ts).
    Preserves line breaks; collapses other whitespace.
    """
    if not text:
        return ""
    s = text

    # 1. Mojibake first (before stripping touches the byte-ish sequences).
    for bad, good in _MOJIBAKE:
        s = s.replace(bad, good)

    # 1a. Em/en dashes: Fish S2.1 Pro reads them with NO pause (they behave
    # like plain spaces). Convert to a comma so TTS gets a natural breath.
    # Absorb surrounding whitespace so "you — what" becomes "you, what".
    s = re.sub(r"\s*[\u2014\u2013]\s*", ", ", s)

    # 1b. Fish Audio bracket prosody cues ([warm], [soft], [with quiet
    # enthusiasm]) — the LLM is prompted to emit them for delivery variety
    # and Fish S2.1-pro renders them as vocal style. They must NEVER reach
    # the transcript/subtitles. Case-insensitive, letters/spaces/hyphens so
    # numeric citations [1] survive. Mirror of client BRACKET_CUE_RE.
    s = re.sub(r"\[[A-Za-z][A-Za-z -]{1,40}\]", "", s)

    # 1c. Hermes steering scaffolding (interruption markers) & orphan brackets —
    # drop entire chunks that are scaffolding, and strip inline markers or orphan ]
    s = re.sub(
        r"\[?\b(?:This response was interrupted by a user correction\.?"
        r"|Visible response before the interruption:?"
        r"|Context from the interrupted assistant response)\]?",
        "",
        s,
        flags=re.IGNORECASE,
    )
    # Strip standalone/orphan brackets like "]" or "[" left over after cue stripping
    s = re.sub(r"^\s*\]\s*|\s*\[\s*$", "", s)

    # 2. Code fences → spoken placeholder; inline code keeps its text.
    s = _CODE_FENCE_RE.sub(" [potongan kode] ", s)
    s = _INLINE_CODE_RE.sub(r"\1", s)

    # 3. Markdown links → link text.
    s = _MD_LINK_RE.sub(r"\1", s)

    # 4. URLs / emails → spoken words.
    s = _URL_RE.sub("link", s)
    s = _EMAIL_RE.sub("alamat email", s)

    # 5. Line-level markdown: hr, headings, bullets, quotes, table pipes.
    s = _HR_RE.sub("", s)
    s = _HEADING_RE.sub("", s)
    # Do NOT strip bullet markers from line start — let them flow so client renders real markdown lists!
    s = _QUOTE_RE.sub("", s)
    s = _TABLE_PIPE_RE.sub(" ", s)

    # 6. Emphasis leftovers.
    s = _EMPHASIS_RE.sub("", s)

    # 7. Emoji, zero-width, control chars (keep \n).
    s = _EMOJI_RE.sub("", s)
    s = _ZERO_WIDTH_RE.sub("", s)
    s = _CONTROL_RE.sub("", s)

    # 8. Normalize repeated punctuation.
    s = _REPEAT_PUNCT_RE.sub(r"\1", s)

    # 8b. Normalize numbers/digits to spoken words (Indonesian/English) for Fish Audio TTS
    try:
        from num2words import num2words

        def _replace_num(match: re.Match) -> str:
            val_str = match.group(0)
            try:
                if "." in val_str:
                    num = float(val_str)
                    return num2words(num, lang="id").replace("point", "koma")
                else:
                    num = int(val_str)
                    return num2words(num, lang="id")
            except Exception:
                return val_str

        # Replace numbers that are not inside bracket citations like [1]
        # Match floats/ints not preceded/followed by brackets
        s = re.sub(r"(?<!\[)\b\d+(?:\.\d+)?\b(?!\])", _replace_num, s)
    except Exception:
        pass

    # 9. Whitespace: newlines → space (voice text is read linearly), collapse
    # runs, cap blank lines, trim. (Newlines must become SPACES, not vanish,
    # or consecutive sentences run together: "Schnee.Semua sistem".)
    s = s.replace("\n", " ")
    s = re.sub(r"[ \t]{2,}", " ", s).strip()
    return s


def _split_sentence(buffer: str) -> tuple[str | None, str]:
    """Split the first complete sentence off the buffer.

    Returns (sentence, rest), or (None, buffer) when no complete sentence is
    available yet. Boundaries: `.`, `!`, `?`, newline. A minimum-length guard
    avoids cutting abbreviations/decimals mid-token; a max-pending cap
    force-cuts at the last space so TTS latency stays bounded.

    Decimal-aware: a `.` preceded AND followed by a digit is NOT a boundary
    (e.g. "3.7", "1.0", "0.54"). This prevents splitting version numbers,
    measurements, and decimal figures mid-token.
    """
    for i, ch in enumerate(buffer):
        if ch in _BOUNDARY_CHARS and (ch == "\n" or i + 1 >= _MIN_SENTENCE_LEN):
            # Decimal guard: don't split on a period that's part of a number.
            # Check the character before and after the period.
            if ch == ".":
                prev_char = buffer[i - 1] if i > 0 else ""
                next_char = buffer[i + 1] if i + 1 < len(buffer) else ""
                if prev_char.isdigit() and next_char.isdigit():
                    continue  # This is a decimal point, not a sentence boundary
                # File extension guard: don't split on file extensions like .py, .ts, .js, .json, .md
                # e.g. "agent.py", "conversation.svelte.ts"
                after = buffer[i + 1 : i + 10]
                if re.match(r"^[a-zA-Z0-9_-]+\b", after) and not re.match(
                    r"^\s", next_char
                ):
                    continue
            return buffer[: i + 1], buffer[i + 1 :]
    if len(buffer) > _MAX_PENDING_LEN:
        cut = buffer.rfind(" ")
        if cut > 0:
            return buffer[:cut], buffer[cut:].lstrip()
        return buffer, ""
    return None, buffer


async def _ws_reader(ws: Any, queue: asyncio.Queue) -> None:
    """Pump WS frames into a queue so the stream loop can race incoming
    events against the filler timeouts (a bare `async for` can't time out)."""
    try:
        async for raw in ws:
            await queue.put(raw)
    except Exception as exc:
        await queue.put(exc)
        return
    await queue.put(None)  # EOF sentinel


def _extract_last_user_text(messages: list) -> str:
    """Pull the last user message text out of provider-format messages."""
    for msg in reversed(messages):
        if msg is None or not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        user_text = ""
        if isinstance(content, list):
            for part in content:
                if part and isinstance(part, dict) and part.get("type") == "text":
                    user_text = part.get("text", "")
                    break
        else:
            user_text = str(content) if content else ""
        if user_text:
            return user_text
    return ""


class HermesLLM(llm.LLM):
    """Custom LLM that bridges to Hermes Agent via WebSocket."""

    def __init__(
        self,
        *,
        ws_url: str = "ws://127.0.0.1:9119/api/ws",
        token: str | None = None,
        model_override: str | None = None,
    ) -> None:
        super().__init__()
        self._ws_url = ws_url
        # Prefer env (set in .env.local); fallback keeps old setups working.
        self._token = token or os.environ.get("HERMES_WS_TOKEN", "")
        self._model_override = model_override
        self._ws: Any = None
        # Room handle for publishing tool-activity events to the UI chip
        # (Gemini/Claude-style "Searching the web…" indicator). Bound by
        # agent.py after session.start().
        self._room: Any = None
        self._session_id: str | None = None
        self._message_id = 0
        # Serializes WS protocol traffic only (short-lived), NOT the full stream
        self._ws_lock = asyncio.Lock()
        # Serializes the whole turn lifecycle (submit + WS read loop). Without
        # this, a second turn submitted while the first is still streaming
        # spawns a second `_ws_reader`, and two concurrent `ws.recv()` calls
        # raise websockets.ConcurrencyError. Holding the lock across submit +
        # read guarantees a single reader at a time; a barged-in turn's stream
        # is cancelled by the framework, releasing the lock for the next turn.
        self._turn_lock = asyncio.Lock()

    def bind_room(self, room: Any) -> None:
        """Attach the LiveKit room so tool-activity events can be published."""
        self._room = room

    def publish_tool_activity(
        self, state: str, tool_name: str, args: dict | None = None
    ) -> None:
        """Best-effort publish of tool start/end to the room.

        The UI renders this as a Gemini/Claude-style activity chip
        ("Searching the web…"). Never fatal: the voice path must keep
        working if the room is gone.
        """
        room = self._room
        if room is None:
            return
        try:
            payload = json.dumps(
                {
                    "type": "jarvis.tool",
                    "state": state,
                    "name": tool_name,
                    "args": args or {},
                }
            )
            asyncio.get_running_loop().create_task(
                room.local_participant.publish_data(payload, reliable=True)
            )
        except Exception:
            logger.debug("publish_tool_activity failed", exc_info=True)

    def publish_turn_state(self, state: str) -> None:
        """Publish turn lifecycle states ('start', 'end') to the client room."""
        room = self._room
        if room is None:
            return
        try:
            payload = json.dumps(
                {
                    "type": "jarvis.turn",
                    "state": state,
                }
            )
            asyncio.get_running_loop().create_task(
                room.local_participant.publish_data(payload, reliable=True)
            )
        except Exception:
            logger.debug("publish_turn_state failed", exc_info=True)

    @property
    def model(self) -> str:
        return "hermes-agent"

    @property
    def provider(self) -> str:
        return "hermes"

    async def _connect(self) -> None:
        if self._ws is not None and self._ws.state is State.OPEN:
            return
        # Close half-open leftovers from a previous failed turn, if any.
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        url = self._ws_url + "?token=" + self._token
        logger.info("Connecting to Hermes at %s", url.replace(self._token, "***"))
        # ping_timeout must comfortably exceed the longest synchronous tool the
        # gateway runs (delegate_task / long terminal calls can hold it busy
        # for a minute+). 20s was too aggressive: a busy gateway misses the
        # pong, websockets closes with 1011, and the next turn dies ("stuck on
        # thinking"). Local socket — a 90s no-pong is genuinely dead.
        self._ws = await websockets.connect(url, ping_interval=20, ping_timeout=90)
        logger.info("Connected to Hermes")

    def _invalidate_connection(self) -> None:
        """Force reconnect + fresh Hermes session on the next turn."""
        ws, self._ws = self._ws, None
        self._session_id = None
        if ws is not None:
            with contextlib.suppress(Exception):
                # Best-effort close so a dead socket doesn't leak fds; the
                # close itself may fail on an already-broken connection.
                asyncio.get_running_loop().create_task(ws.close())

    async def _ensure_session(self) -> str:
        if self._session_id:
            if self._ws is not None and self._ws.state is State.OPEN:
                return self._session_id
            # Socket died mid-session (observed: keepalive 1011 while the
            # gateway was busy with long tools). Returning the cached
            # session_id here is what bricked every following turn with
            # ConnectionClosedError ("stuck on thinking"). Reconnect fresh.
            logger.warning(
                "Hermes WS no longer open (state=%s) — reconnecting with a fresh session",
                getattr(self._ws, "state", None),
            )
            self._invalidate_connection()

        async with self._ws_lock:
            await self._connect()
            assert self._ws is not None
            ws = self._ws

            # Wait for gateway.ready
            async for raw in ws:
                data = json.loads(raw)
                if (
                    data.get("method") == "event"
                    and data.get("params", {}).get("type") == "gateway.ready"
                ):
                    break

            # Create session
            self._message_id += 1
            create_id = self._message_id
            params: dict[str, Any] = {
                "title": f"livekit-{asyncio.get_event_loop().time()}"
            }
            if self._model_override:
                params["model"] = self._model_override
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": create_id,
                        "method": "session.create",
                        "params": params,
                    }
                )
            )

            async for raw in ws:
                data = json.loads(raw)
                if data.get("id") == create_id:
                    if "error" in data:
                        raise RuntimeError(f"Session create failed: {data['error']}")
                    self._session_id = data["result"]["session_id"]
                    break

            # Activate session
            self._message_id += 1
            activate_id = self._message_id
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": activate_id,
                        "method": "session.activate",
                        "params": {"session_id": self._session_id},
                    }
                )
            )

            async for raw in ws:
                data = json.loads(raw)
                if data.get("id") == activate_id:
                    break

        logger.info("Hermes session created: %s", self._session_id)
        return self._session_id

    def chat(
        self,
        *,
        chat_ctx: ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: Any = None,
        parallel_tool_calls: Any = None,
        tool_choice: Any = None,
        extra_kwargs: Any = None,
    ) -> LLMStream:
        if conn_options is None:
            conn_options = DEFAULT_API_CONNECT_OPTIONS
        return HermesLLMStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
        )

    async def aclose(self) -> None:
        if self._ws:
            await self._ws.close()


class HermesLLMStream(LLMStream):
    """LLMStream implementation for Hermes Agent with voice formatting:
    Lapis 1 (instructions prepend), Lapis 2 (sentence cleaner),
    Lapis 3 (anti-silence filler engine)."""

    async def _run(self) -> None:
        hermes = self._llm
        loop = asyncio.get_running_loop()
        session_id = await hermes._ensure_session()

        # NOTE: to_provider_format returns a tuple (messages_list, tools)
        messages, _tools = self._chat_ctx.to_provider_format(format="openai")
        user_text = _extract_last_user_text(messages)
        if not user_text:
            logger.warning("No user text found in chat context")
            return

        # Serialize the whole turn (submit + read loop) so overlapping turns
        # never spawn concurrent `_ws_reader` tasks on the same socket.
        async with hermes._turn_lock:
            await self._run_turn(hermes, session_id, user_text, loop)

    async def _run_turn(
        self,
        hermes: "HermesLLM",
        session_id: str,
        user_text: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        # Lapis 1: prepend voice instructions (LiveKit `instructions=` never
        # reach Hermes — verified in code).
        submit_text = VOICE_INSTRUCTIONS + "\n\n" + user_text
        logger.info("Sending to Hermes: %s", user_text[:80])

        hermes._message_id += 1
        submit_id = hermes._message_id

        t_submit = loop.time()

        async with hermes._ws_lock:
            await hermes._ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": submit_id,
                        "method": "prompt.submit",
                        "params": {"session_id": session_id, "text": submit_text},
                    }
                )
            )

        # Reader task → queue so we can drain WS events deterministically.
        queue: asyncio.Queue = asyncio.Queue()
        reader = asyncio.create_task(_ws_reader(hermes._ws, queue))

        # Smart Filler Engine v6 — manages opening/dwell fillers per turn.
        # Defined before send_text/send_filler so closures can reference it.
        filler = _FillerEngine(loop)

        async def send_text(text: str, *, flush_after: bool = False) -> None:
            nonlocal spoken_any, t_last_spoken, pending_text
            spoken_any = True
            t_last_spoken = loop.time()
            filler.record_spoken()
            # Always terminate chunks on whitespace: the voice pipeline
            # concatenates deltas, so "satu." + "Dua" would become "satu.Dua"
            # — Fish TTS spells the glued token letter-by-letter and the
            # transcript shows the missing space.
            await self._event_ch.send(
                ChatChunk(
                    id="hermes",
                    delta=ChoiceDelta(role="assistant", content=text + " "),
                )
            )
            logger.info("TTS chunk: %.80s", text)
            if flush_after:
                await flush_segment()

        async def flush_segment() -> None:
            """Force the pipeline to close the current speech segment NOW.

            The TTS sentence tokenizer holds the LAST sentence until more
            text arrives or the turn ends — the exact reason fillers and
            the opening sentence used to be glued onto the final answer
            (verified: probe_tts_streaming.py / probe_tokenizer.py). A
            FlushSentinel ends the segment: its TTS channel closes and
            synthesis starts immediately.
            """
            nonlocal pending_text
            pending_text = False
            await self._event_ch.send(_VoiceFlush())

        got_ack = False
        turn_over = False
        sentence_buffer = ""
        spoken_any = False
        pending_text = False  # text emitted since the last FlushSentinel
        t_last_spoken = loop.time()
        t_first_delta: float | None = None
        t_first_sentence: float | None = None

        async def send_filler(text: str) -> None:
            """Send a filler line to TTS immediately, bypassing the LLM.

            The filler is sent as a normal ChatChunk + FlushSentinel, so
            the SDK routes it straight to TTS synthesis without waiting
            for the LLM stream to finish. This is the LiveKit-recommended
            pattern for masking dead air during tool execution.
            """
            nonlocal spoken_any, t_last_spoken, pending_text
            spoken_any = True
            t_last_spoken = loop.time()
            filler.record_spoken()
            # Cancel any pending filler task — we just spoke, so no need
            # for a scheduled filler to fire.
            filler.cancel_pending()
            await self._event_ch.send(
                ChatChunk(
                    id="hermes",
                    delta=ChoiceDelta(role="assistant", content=text + " "),
                )
            )
            await self._event_ch.send(_VoiceFlush())
            pending_text = False

        try:
            # Phase 1 — wait for the submit ack and DROP every event that
            # arrives before it. When LiveKit barges in (user re-speaks),
            # the previous stream task is cancelled mid-read and its
            # leftover events stay in the socket buffer. Without this
            # drain, the next turn reads the OLD turn's message.delta /
            # message.complete and "completes" in 0.01s with no real
            # answer — the exact "user has to repeat themselves" turns in
            # the log (repeated `ack=False, total=0.01s`). The gateway
            # always returns the RPC ack before emitting events for a new
            # turn, so anything pre-ack is stale by definition.
            dropped_stale = 0
            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=SUBMIT_ACK_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    hermes._invalidate_connection()
                    raise RuntimeError(
                        f"Hermes submit ack not received in {SUBMIT_ACK_TIMEOUT}s"
                    ) from None

                if isinstance(item, BaseException):
                    hermes._invalidate_connection()
                    raise RuntimeError(f"Hermes WS error: {item}") from item
                if item is None:  # EOF sentinel
                    hermes._invalidate_connection()
                    raise RuntimeError("Hermes WS closed mid-turn")

                try:
                    data = json.loads(item)
                except json.JSONDecodeError:
                    continue

                if data.get("id") == submit_id:
                    if "error" in data:
                        raise RuntimeError(f"Submit failed: {data['error']}")
                    got_ack = True
                    logger.info("Hermes submit ack: %s", data.get("result"))
                    hermes.publish_turn_state("start")
                    # Schedule early acknowledge filler at 2.0s to prevent dead air
                    # if the LLM provider experiences queue/latency spikes.
                    await filler.schedule_early_ack(send_filler)
                    if dropped_stale:
                        logger.info(
                            "Drained %d stale event(s) from the previous turn",
                            dropped_stale,
                        )
                    break

                stale_type = (data.get("params") or {}).get("type", "?")
                dropped_stale += 1
                logger.debug("Dropping stale pre-ack event: %s", stale_type)

            # Phase 2 — stream the real turn. A legitimate turn ALWAYS emits
            # some content signal (message.start for a fresh turn — see the
            # gateway's _run_prompt_submit — or a delta for a redirected
            # turn) before its terminator. So a message.complete/turn_end
            # that arrives with no preceding content is a leftover terminator
            # from a barged-in turn; dropping it prevents the 0.01s fake
            # completions that forced the user to repeat themselves.
            seen_turn_signal = False
            while not turn_over:
                item = await queue.get()

                if isinstance(item, BaseException):
                    hermes._invalidate_connection()
                    raise RuntimeError(f"Hermes WS error: {item}") from item
                if item is None:  # EOF sentinel
                    hermes._invalidate_connection()
                    raise RuntimeError("Hermes WS closed mid-turn")

                try:
                    data = json.loads(item)
                except json.JSONDecodeError:
                    continue

                # JSON-RPC responses for other ids (e.g. a queued follow-up
                # submit's ack) — ignore, not this turn's concern.
                if data.get("id") is not None:
                    continue

                if data.get("method") != "event":
                    continue

                params = data.get("params", {})
                event_type = params.get("type")
                payload = params.get("payload", {}) or {}

                if event_type == "message.start":
                    # Fresh-turn bracket from the gateway — everything from
                    # here belongs to THIS submit.
                    seen_turn_signal = True
                elif event_type == "message.delta":
                    delta = payload.get("text", "")
                    if delta:
                        # A delta is content even before message.start was
                        # observed (redirected turns stream straight in).
                        seen_turn_signal = True
                        if t_first_delta is None:
                            t_first_delta = loop.time()
                            logger.info(
                                "Hermes TTFT: %.2fs after submit",
                                t_first_delta - t_submit,
                            )
                        sentence_buffer += delta
                        # Lapis 2: cut complete sentences, clean, yield.
                        while True:
                            sentence, sentence_buffer = _split_sentence(sentence_buffer)
                            if sentence is None:
                                break
                            # Hermes steering scaffolding (interruption markers)
                            # can leak into the reply stream after a redirected
                            # turn — never speak/transcribe it. Check BEFORE
                            # send_text: in HEAD the check ran after the text
                            # had already been streamed to TTS (dead code).
                            if contains_scaffold(sentence):
                                logger.info(
                                    "Dropped Hermes steering scaffold from reply stream"
                                )
                                continue
                            # Skip newline-only chunks — they create paragraph
                            # breaks in the client UI and serve no purpose
                            # for TTS. Only process chunks with actual content.
                            if not sentence.strip():
                                continue
                            # Approach D (Hybrid): Send first sentence
                            # IMMEDIATELY — it doubles as the opening filler.
                            # The filler engine will cancel its own opening
                            # filler if LLM already emitted text (see
                            # tool.generating handler). This gives the fastest
                            # possible response with zero added latency.
                            #
                            # CRITICAL: flush_after=True ONLY for the first
                            # sentence — it forces the TTS to render the
                            # opening as its own segment, preventing it from
                            # being glued onto the final answer. Subsequent
                            # sentences flow WITHOUT flush so the TTS sentence
                            # tokenizer batches them naturally (no paragraph-
                            # like gaps in the client UI).
                            #
                            # MULTI-TOOL SUPPRESSION: If ANY tool is actively
                            # running (filler.has_active_tools), do NOT send LLM
                            # text to TTS. The LLM may emit "Let me check..."
                            # for every tool, which sounds robotic. We only
                            # want the first opening sentence, then silence
                            # until all tools complete.
                            if filler.has_active_tools and t_first_sentence is not None:
                                # Tools are running and we already sent opening —
                                # buffer this text for later, don't send to TTS.
                                logger.info(
                                    "Suppressing LLM text during tool execution: %.40s",
                                    sentence,
                                )
                                pending_text = True
                                continue
                            else:
                                logger.info(
                                    "NOT suppressing: has_active=%s, first_sent=%s, text=%.40s",
                                    filler.has_active_tools,
                                    t_first_sentence is not None,
                                    sentence,
                                )
                            if t_first_sentence is None:
                                t_first_sentence = loop.time()
                                logger.info(
                                    "First sentence to TTS: %.2fs after submit (TTFT delta: %.2fs)",
                                    t_first_sentence - t_submit,
                                    t_first_delta - t_submit if t_first_delta else 0,
                                )
                                # Flush after first sentence if this is an opening filler
                                await send_text(
                                    sentence,
                                    flush_after=True,
                                )
                            else:
                                await send_text(sentence)
                            pending_text = True
                elif event_type == "thinking.delta":
                    seen_turn_signal = True
                    # Thinking produces NO audio for the user — keep the
                    # silence timer running (v1 wrongly treated thinking
                    # activity as user-perceptible activity).
                elif event_type == "tool.generating":
                    seen_turn_signal = True
                    tool_name = payload.get("name", "?")
                    tool_args = payload.get("args", {})
                    logger.info(
                        "Hermes tool started: %s (args=%s)", tool_name, tool_args
                    )

                    # INSTANT PRE-TOOL FLUSH: If the LLM has emitted an opening sentence
                    # (or partial sentence in buffer), flush it to TTS immediately before
                    # the tool executes so speech starts with zero delay.
                    if sentence_buffer.strip():
                        sentence = sentence_buffer.strip()
                        sentence_buffer = ""
                        if t_first_sentence is None:
                            t_first_sentence = loop.time()
                            logger.info(
                                "Flushing LLM opening sentence before tool: %.60s",
                                sentence[:60],
                            )
                            await send_text(sentence, flush_after=True)
                        else:
                            await send_text(sentence)
                        pending_text = True

                    if pending_text:
                        logger.info("Flushing pending text before tool: %s", tool_name)
                        await flush_segment()

                    # Smart Filler Engine: decide whether to speak a fallback filler.
                    # - If LLM already emitted first sentence (t_first_sentence
                    #   is not None), that sentence doubles as the opening
                    #   filler — cancel the engine's own opening filler to
                    #   prevent double-speak.
                    # - If no LLM text yet, schedule opening filler with 0.3s
                    #   gate (fast tools skip it entirely).
                    is_first_tool = filler.record_tool_start(tool_name)
                    filler.reset_dwell()

                    if is_first_tool:
                        if t_first_sentence is None:
                            await filler.schedule_opening(send_filler)
                        else:
                            logger.info(
                                "LLM already emitted opening sentence, "
                                "skipping engine filler for tool: %s",
                                tool_name,
                            )
                    else:
                        # Subsequent tools: schedule dwell filler for extended
                        # silence during multi-tool.
                        await filler.schedule_dwell(send_filler)

                    # Publish tool activity to UI (chip indicator)
                    hermes.publish_tool_activity("start", tool_name, tool_args)
                elif event_type == "tool.complete":
                    if not seen_turn_signal:
                        continue  # stale tail of the barged-in turn
                    logger.info("Hermes tool complete")
                    # Tool finished — cancel any pending filler that hasn't
                    # fired yet. If the filler already fired (tool was slow),
                    # cancel_pending() is a no-op.
                    filler.cancel_pending()
                    filler.record_tool_end()
                    # Flush any LLM text that was buffered while the tool was
                    # running. This is the actual answer content that the LLM
                    # produced before or during the tool call.
                    if sentence_buffer.strip():
                        logger.info(
                            "Flushing buffered text after tool: %.60s…",
                            sentence_buffer[:60],
                        )
                        await send_text(sentence_buffer)
                        sentence_buffer = ""
                        pending_text = True
                    hermes.publish_tool_activity("complete", "?")
                elif event_type in (
                    "message.complete",
                    "turn.complete",
                    "session.turn_end",
                ):
                    if not seen_turn_signal:
                        # Leftover terminator from a barged-in turn — ignore
                        # it, the real turn's own message.start is coming.
                        logger.info(
                            "Dropping stale %s before any turn signal",
                            event_type,
                        )
                        continue
                    logger.info(
                        "Hermes turn complete (event=%s, ack=%s, total=%.2fs)",
                        event_type,
                        got_ack,
                        loop.time() - t_submit,
                    )
                    # Flush any buffered first sentence for no-tool turns.
                    # If a tool was active, this was already flushed on
                    # tool.complete. This is the safety net for turns that
                    # never triggered a tool.
                    if sentence_buffer.strip():
                        logger.info(
                            "Flushing buffered text at turn end: %.60s…",
                            sentence_buffer[:60],
                        )
                        await send_text(sentence_buffer)
                        sentence_buffer = ""
                    hermes.publish_turn_state("complete")
                    turn_over = True
                elif event_type == "error":
                    # An error event IS a turn signal: the gateway emits
                    # error + message.complete (no message.start) when a
                    # turn fails at startup — observed with an fd-exhausted
                    # gateway: 'Error: ... Too many open files'. Dropping it
                    # leaves the user in dead silence ("stuck on thinking").
                    seen_turn_signal = True
                    logger.error("Hermes error event: %s", payload)
                    if spoken_any is False:
                        # Nothing was said yet — announce the failure instead
                        # of ending in silence.
                        await send_text(
                            "[calm] Sorry, something failed on my end. "
                            "Can you try that again?",
                            flush_after=True,
                        )
                    turn_over = True
        finally:
            reader.cancel()
            filler.cancel_pending()

        # Flush any trailing partial sentence.
        if sentence_buffer.strip():
            await send_text(sentence_buffer)
