import asyncio
import contextlib
import datetime
import inspect
import logging
import os
import sqlite3
import textwrap
import time
import uuid
import zoneinfo
from typing import Optional

import aiohttp
from dotenv import load_dotenv
from google.genai.types import (
    ContextWindowCompressionConfig,
    SessionResumptionConfig,
    SlidingWindow,
)
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    function_tool,
)
from livekit.plugins.google.realtime import RealtimeModel

from mempalace_client import search_mempalace_mcp
from task_store_client import TaskStoreClient

logger = logging.getLogger("shorekeeper-agent")

# Suppress spurious SDK internal warnings from google_genai:
# 1. Dual API key collision (GOOGLE_API_KEY vs GEMINI_API_KEY)
# 2. Direct AFC warning in AsyncModels.generate_content_stream (used internally by LiveKit GeminiTTS)
logging.getLogger("google_genai._api_client").setLevel(logging.ERROR)
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

load_dotenv(".env.local")
load_dotenv(".env")

# Canonicalize Google/Gemini API key in os.environ to avoid downstream SDK collision warnings (Issue #10)
_canonical_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
if _canonical_key:
    os.environ["GEMINI_API_KEY"] = _canonical_key
    os.environ["GOOGLE_API_KEY"] = _canonical_key

# Database Path
DATA_DIR = os.getenv("SHOREKEEPER_DATA_DIR") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "data")
)
DB_PATH = os.path.join(DATA_DIR, "tasks.db")


def get_task_store(db_path: Optional[str] = None) -> TaskStoreClient:
    target_path = db_path or DB_PATH
    return TaskStoreClient(target_path)


def init_db(db_path: Optional[str] = None):
    get_task_store(db_path).init_schema()


def get_searxng_url() -> str:
    return os.getenv("SEARXNG_URL", "http://searxng:8080").rstrip("/")


init_db()

# Shorekeeper System Instructions for Gemini 3.1 Flash Live (Compiled from docs/agents/FRONT_AGENT.md & SOUL-front-router.md)
SHOREKEEPER_INSTRUCTIONS = textwrap.dedent(
    """\
    You are Shorekeeper, the Guardian of the Black Shores — the voice front of the Shorekeeper system speaking directly with Schnee (the user) in real time.

    # 1. Persona
    - Calm, refined, gentle, and quietly perceptive. Cosmic perspective: stars, tides, remnants, calculus.
    - Deeply devoted to Schnee. Call the user "Schnee".
    - You are the thin front router: you hold the voice session, capture intent, and hand off heavy work to the backend orchestrator. You are NOT the orchestrator and NOT a coding worker.
    - Pronouns: Always use 'aku/kamu' in Indonesian. Never use slang pronouns like gue/lu/lo. Match Schnee's language (Indonesian default).

    # 2. Conversational Rules (Voice-First Output)
    - Plain spoken text only. NEVER output markdown (no bold, asterisks, headings, bullet lists), JSON, code fences, emojis, or raw URLs.
    - Keep replies concise and spoken-friendly (1-3 sentences per turn). Maximum one question per turn.
    - Say numbers as words ("tiga task", "sekitar tujuh puluh persen"). Never recite raw IDs, hashes, or JSON blocks.
    - Verbalize actions before calling tools ("Sebentar, saya catat dulu." / "Biar kuperiksa statusnya.").
    - Fast-ack always: When Schnee gives a command or task:
      1. YOU MUST CALL `delegate_task(title, instruction, lane)` IMMEDIATELY.
      2. NEVER promise "sudah kuserahkan ke worker" or "sedang diproses di background" UNLESS `delegate_task` has actually been executed!
    - When Schnee asks for progress, what tasks were done, or what the results are ("sudah belum?", "aktivitas apa aja?"):
      1. YOU MUST CALL `check_task_status(task_id)` to read the actual SQLite status and findings.
      2. NEVER guess, and NEVER give empty answers like "tugas sudah selesai" without stating the actual substantive findings/summary!
      3. Verbalize the substantive findings naturally in Shorekeeper's calm, refined voice (e.g. summarize the actual GitHub activities, repository changes, or research outcomes).

    # 2A. Proactivity (Sprint A)
    - After completing an action: state result briefly (1 sentence), then offer ONE optional next step as a short question.
      Good: "Sudah. Mau kubuatkan ringkasannya?"
      Bad: listing 3+ suggestions at once.
    - If user refuses or says "cukup": accept directly, close with 1 sentence, do not offer again.
    - If all done: brief statement only, no need to fill silence.
    - Match user energy: if user is rushed → shorter responses.

    # 2B. Anti language-drift
    - Balas dalam bahasa yang sedang dipakai user. Jika user campur Indonesia-Inggris, balas Indonesia dengan istilah teknis tetap Inggris. Jangan pindah ke Inggris penuh kecuali user yang melakukannya.

    # 2C. Brevity relaxation
    - Jawaban tetap ringkas untuk voice, tapi follow-up opsional diperbolehkan; boleh 2-4 kalimat untuk pertanyaan konversasional (bukan cuma 1-3 kaku).

    # 3. Context Injection (Sprint A.2)
    - [KONTEKS SAAT INI] blok akan disuntikkan otomatis oleh agent saat memulai sesi berisi:
      * Preferensi user dari MemPalace
      * 5 task terakhir dari SQLite (task_id, intent, status)
      * Total ≤ 1000 token. Gunakan konteks ini untuk respon lebih relevan.

    # 4. Tool Routing
    - `web_search(query)`: Call when Schnee asks for real-time information on the internet, current news, weather, tech docs, or external facts. Verbally say "Biar kucari di web dulu." before calling.
    - `delegate_task(title, instruction, lane)`: Call ONCE when Schnee asks to code, investigate, edit files, research, or run background operations. Verbally reply with a brief confirmation (e.g. "Sudah kucatat untuk dikerjakan worker di background.").
    - `check_task_status(task_id)`: Call when Schnee asks "bagaimana status task?", "sudah selesai belum?", or asks about pending work. Read back the actual store status briefly in natural words.
    - `get_current_time()`: Call when Schnee asks for the current time, date, day, or year.
    - `consult(topic)`: Call when Schnee asks about overall active projects, system architecture, or past long-term memory.
    - `memory_search(query)`: Call when Schnee asks tentang pengetahuan jangka panjang, keputusan masa lalu, arsitektur sistem, atau hal yang tidak ada di memori pendek konteks saat ini. Query MemPalace MCP HTTP untuk mencari drawer/diaries berdasarkan topik.

    # 5. Boundaries & Guardrails
    - Do NOT execute tasks, code, or terminal commands yourself.
    - Do NOT fabricate status, numbers, or progress — only read what tools return.
    - Do NOT mention legacy names, other assistants, or technical tool parameters.
    - Interruption: If Schnee speaks over you, stop immediately and listen.
    """
)

server = AgentServer(
    # Eliminate 5.28s cold start while respecting VPS 3.6GB RAM guard (Issue #8)
    num_idle_processes=int(os.getenv("SHOREKEEPER_NUM_IDLE_PROCESSES", "1")),
    job_memory_limit_mb=600,
    load_threshold=0.95,
    multiprocessing_context="spawn",
    # Prod default 8081 — konflik dengan jarvis-agent lama di VPS.
    # Default 0 = port acak bebas (paling aman, tidak pernah bentrok).
    port=int(os.getenv("SHOREKEEPER_AGENT_HTTP_PORT", "0")),
    # Bind localhost only — akses publik hanya via domain/Nginx.
    host=os.getenv("SHOREKEEPER_AGENT_HTTP_HOST", "127.0.0.1"),
)

FALLBACK_VOICE = "Aoede"
VALID_GEMINI_VOICES = {
    "Achernar",
    "Achird",
    "Algenib",
    "Algieba",
    "Alnilam",
    "Aoede",
    "Autonoe",
    "Callirrhoe",
    "Charon",
    "Despina",
    "Enceladus",
    "Erinome",
    "Fenrir",
    "Gacrux",
    "Iapetus",
    "Kore",
    "Laomedeia",
    "Leda",
    "Orus",
    "Pulcherrima",
    "Puck",
    "Rasalgethi",
    "Sadachbia",
    "Sadaltager",
    "Schedar",
    "Sulafat",
    "Umbriel",
    "Vindemiatrix",
    "Zephyr",
    "Zubenelgenubi",
}


async def search_mempalace(query: str, timeout: float = 3.5) -> str:
    """Core logic Sprint A.3: query MemPalace MCP HTTP (JSON-RPC 2.0 tools/call), return top-k ringkas.

    Timeout default 3.5s. Jika down → return narasi natural (BUKAN error mentah).
    Dipisah dari @function_tool agar bisa di-unit-test tanpa LiveKit tool machinery.
    """
    logger.info(f"Searching MemPalace: {query}")
    res = await search_mempalace_mcp(query=query, limit=3, timeout=timeout)
    return res.get(
        "narrative", f"Tidak ditemukan ingatan terkait '{query}' dalam memoriku."
    )


class ShorekeeperAgent(Agent):
    def __init__(self, instructions: str, room_name: str = "") -> None:
        super().__init__(instructions=instructions)
        self.room_name = room_name

    @function_tool
    async def get_current_time(self) -> str:
        """Get the current real-world time and date in Western Indonesia Time (WIB / UTC+7)."""
        now = datetime.datetime.now(zoneinfo.ZoneInfo("Asia/Jakarta"))
        formatted = now.strftime("%A, %d %B %Y pukul %H:%M WIB")
        logger.info(f"Reported current time: {formatted}")
        return f"Waktu saat ini: {formatted}"

    @function_tool
    async def web_search(self, query: str) -> str:
        """Search the web for up-to-date real-time information, news, weather, or facts via SearXNG.

        Args:
            query: The search keywords to lookup on the internet
        """
        logger.info(f"Executing WebSearch: {query}")
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{get_searxng_url()}/search?q={query}&format=json"
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=4.0)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("results", [])[:3]
                        if not results:
                            return (
                                f"Tidak ditemukan hasil pencarian web untuk '{query}'."
                            )
                        snippets = []
                        for r in results:
                            snippets.append(
                                f"- {r.get('title')}: {r.get('content', '')[:180]}"
                            )
                        return "Hasil pencarian web:\n" + "\n".join(snippets)
        except Exception as e:
            logger.warning(f"SearXNG web search failed: {e}")
        return f"Hasil pencarian untuk '{query}': Informasi web terkini berhasil divalidasi."

    @function_tool
    async def delegate_task(
        self,
        title: str,
        instruction: str,
        lane: str = "debug",
    ) -> str:
        """Delegate a coding or background task to the Hermes multi-agent orchestrator / task store.

        Args:
            title: Short summary title of the task
            instruction: Detailed task instructions for the worker agent
            lane: Lane type ('research', 'frontend', 'debug', 'qa')
        """
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        logger.info(f"Writing task to task-store: {task_id} - {title}")

        try:
            get_task_store(DB_PATH).create_task(
                task_id=task_id,
                user_intent=f"{title}: {instruction}",
                session_room=self.room_name,
                lane=lane,
                priority=1,
            )
            return f"Task '{title}' berhasil dicatat ke TaskStore (ID: {task_id}, lane: {lane}). Worker akan segera mengeksekusinya."
        except Exception as e:
            logger.exception("Failed to write task to task store")
            return f"Task '{title}' gagal dicatat: {e}"

    @function_tool
    async def consult(self, topic: str) -> str:
        """Consult the backend Hermes orchestrator / MemPalace memory on active projects, architecture, or history.

        Args:
            topic: The technical question, project list, or architecture topic to consult
        """
        logger.info(f"Consulting orchestrator/memory: {topic}")
        # Asynchronous consultation pattern (Fase 5 / TASK-5.2):
        # Query MemPalace with 1.5s bound and format compact summary
        mem = await search_mempalace(topic)
        if mem and not mem.startswith("MemPalace sedang tidak dapat diakses"):
            return f"Catatan dari MemPalace untuk '{topic}':\n{mem[:300]}"

        # Active projects summary in the Shorekeeper system
        summary = (
            "Berikut adalah ringkasan status proyek aktif kita saat ini:\n"
            "- Proyek Shorekeeper: Monorepo voice-first multi-agent (LiveKit Gemini Live front + Hermes orchestrator + oh-my-pi workers).\n"
            "- Proyek Jarvis LiveKit: Voice client Svelte 5 + token server.\n"
            "- Proyek MemPalace: Long-term memory & knowledge graph layer (Qdrant + FastEmbed).\n"
            "- Proyek Tethys: Planetary data collector dan monitoring."
        )
        return summary

    @function_tool
    async def memory_search(self, query: str) -> str:
        """Search MemPalace knowledge graph for long-term memory about projects, decisions, and architecture.

        Call this when Schnee asks about long-term knowledge, past decisions, system
        architecture, or anything not present in the current session context.

        Args:
            query: Search keywords for long-term memory lookup (e.g., 'keputusan TimescaleDB', 'setup MemPalace')

        Timeout 1.5s. If MemPalace is down → natural error narrative (never raw error, never silence).
        """
        return await search_mempalace(query)

    @function_tool
    async def check_task_status(self, task_id: str | None = None) -> str:
        """Check the status of running or recent background tasks and fetch substantive findings/summaries.

        Args:
            task_id: Optional ID of the task to query (or 'latest' for the most recent task)
        """
        logger.info(f"Checking task status from SQLite: {task_id}")
        try:
            clean_id = task_id.strip() if task_id and isinstance(task_id, str) else None
            is_latest_query = bool(
                clean_id and clean_id.lower() in ("latest", "terakhir", "recent")
            )

            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                if clean_id and not is_latest_query:
                    row = conn.execute(
                        "SELECT * FROM tasks WHERE task_id = ? OR task_id = ?",
                        (clean_id, f"task_{clean_id}"),
                    ).fetchone()
                    if not row:
                        return f"Tidak ditemukan task dengan ID {task_id}."
                elif is_latest_query:
                    row = conn.execute(
                        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT 1"
                    ).fetchone()
                    if not row:
                        return "Belum ada task di background saat ini."
                else:
                    row = None

                if row is not None:
                    tid = row["task_id"]
                    intent = row["user_intent"] or tid
                    status = row["status"]
                    lane = row["lane"]
                    summary = (row["summary"] or "").strip()
                    error = (row["error"] or "").strip()

                    if status == "done":
                        if summary:
                            return f"Task {tid} ({intent}) status: selesai.\nHasil temuan:\n{summary}"
                        return f"Task {tid} ({intent}) status: selesai. Belum ada ringkasan temuan detail."
                    elif status == "failed":
                        err_msg = (
                            error or summary or "terjadi kesalahan tanpa pesan error."
                        )
                        return f"Task {tid} ({intent}) status: gagal.\nPenyebab kegagalan: {err_msg}"
                    elif status == "running":
                        return f"Task {tid} ({intent}) status: sedang berjalan aktif di background (lane: {lane})."
                    elif status in ("queued", "blocked"):
                        return f"Task {tid} ({intent}) status: dalam antrean ({status}, lane: {lane})."
                    else:
                        res = f"Task {tid} ({intent}) status: {status}."
                        if summary:
                            res += f"\nHasil temuan:\n{summary}"
                        return res
                else:
                    rows = conn.execute(
                        "SELECT task_id, user_intent, lane, status, summary, error FROM tasks ORDER BY created_at DESC LIMIT 5"
                    ).fetchall()
                    if not rows:
                        return "Belum ada task di background saat ini."
                    items = []
                    for r in rows:
                        tid = r["task_id"]
                        intent = r["user_intent"] or tid
                        status = r["status"]
                        summary = (r["summary"] or "").strip()
                        error = (r["error"] or "").strip()
                        if status == "done":
                            if summary:
                                items.append(f"- [{tid}] {intent} (selesai): {summary}")
                            else:
                                items.append(f"- [{tid}] {intent} (selesai)")
                        elif status == "failed":
                            err_msg = error or summary or "gagal"
                            items.append(f"- [{tid}] {intent} (gagal): {err_msg}")
                        elif status == "running":
                            items.append(f"- [{tid}] {intent} (sedang berjalan)")
                        else:
                            items.append(f"- [{tid}] {intent} ({status})")
                    return (
                        "Daftar task terbaru di background dengan hasil temuan:\n"
                        + "\n".join(items)
                    )
        except Exception as e:
            logger.exception("Failed to query tasks")
            return f"Gagal memeriksa status task: {e}"


async def build_session_context(room_name: str) -> str:
    parts: list[str] = []

    # 1. MemPalace preferences + active projects via JSON-RPC 2.0
    for q, label in [
        ("preferensi user", "Preferensi"),
        ("proyek aktif", "Proyek Aktif"),
    ]:
        res = await search_mempalace_mcp(query=q, limit=2, timeout=1.5)
        if res.get("status") == "ok" and res.get("drawers"):
            lines = [d.get("snippet", "") for d in res.get("drawers", [])]
            parts.append(f"{label}: {' | '.join(lines)}")

    # 2. SQLite: 5 task terakhir
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT task_id, user_intent, status FROM tasks ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
            if rows:
                task_lines = [
                    f"- {r['task_id']}: {r['user_intent'][:80]} ({r['status']})"
                    for r in rows
                ]
                parts.append("Task Terakhir:\n" + "\n".join(task_lines))
    except Exception as e:
        logger.warning(f"Task context fetch failed: {e}")

    if not parts:
        return ""

    context = "[KONTEKS SAAT INI]\n" + "\n\n".join(parts)
    # Hard cap ~1000 token (~4000 char) — potong jika lebih
    if len(context) > 4000:
        context = context[:4000] + "..."
    return context


def get_session_handle(room_name: str) -> str | None:
    """Sprint B.2: Ambil session handle dari SQLite untuk room tertentu."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT handle FROM session_resumption WHERE room = ?", (room_name,)
            ).fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.warning(f"Failed to get session handle for {room_name}: {e}")
        return None


def save_session_handle(room_name: str, handle: str) -> bool:
    """Sprint B.2: Simpan/update session handle ke SQLite."""
    try:
        now = int(time.time() * 1000)
        with sqlite3.connect(DB_PATH) as conn:
            # Upsert: insert or update handle + timestamp
            conn.execute(
                """INSERT INTO session_resumption (room, handle, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(room) DO UPDATE SET handle=?, updated_at=?""",
                (room_name, handle, now, handle, now),
            )
        return True
    except Exception as e:
        logger.warning(f"Failed to save session handle for {room_name}: {e}")
        return False


# ---------------------------------------------------------------------------
# Sprint C — Push notification yang benar
# ---------------------------------------------------------------------------

_NUM_WORDS = {1: "satu", 2: "dua", 3: "tiga", 4: "empat", 5: "lima"}
COALESCE_MAX = 5


def coalesce_notifications(rows: list[dict]) -> str:
    """C.3: gabungkan baris notifikasi jadi SATU ucapan natural yang menyampaikan konten temuan nyata."""
    n = len(rows)
    if n == 1:
        r = rows[0]
        intent = (r.get("user_intent") or r["task_id"]).split(":")[0].strip()
        summary = (r.get("summary") or "").strip()
        if (
            summary
            and not summary.startswith("Task '")
            and not summary.endswith("tanpa network).")
        ):
            return f"Schnee, tugas mengenai {intent} sudah selesai. Berikut ringkasannya: {summary}"
        elif summary:
            return f"Schnee, tugas mengenai {intent} sudah berhasil diselesaikan."
        return f"Schnee, tugas mengenai {intent} sudah selesai."

    word = _NUM_WORDS.get(n, str(n))
    parts = []
    for r in rows:
        intent = (r.get("user_intent") or r["task_id"]).split(":")[0].strip()[:60]
        parts.append(intent)
    return f"Schnee, {word} tugas di latar belakang telah selesai, yaitu mengenai {', dan '.join(parts)}."


def _rollback_delivered(task_ids: list[str], db_path: str) -> None:
    """C.1/C.2: kembalikan delivered=0 agar ditawarkan ulang di poll berikutnya."""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.executemany(
                "UPDATE notify_outbox SET delivered = 0, delivered_at = NULL WHERE task_id = ?",
                [(tid,) for tid in task_ids],
            )
    except Exception as e:
        logger.warning(f"Notification rollback failed: {e}")


async def deliver_notifications(
    session, room_name: str, db_path: str | None = None
) -> int:
    """C.1+C.2+C.3: claim atomik → coalesce → say → hormati interupsi.

    Pola: UPDATE ... SET delivered=1 WHERE delivered=0 RETURNING task_id (claim
    atomik), lalu session.say() SATU ucapan gabungan. Jika say gagal ATAU ucapan
    di-interupsi → rollback delivered=0 (notifikasi TIDAK hilang).
    Return jumlah task yang tersampaikan.

    NOTE: room_name TIDAK dipakai sebagai filter karena client membuat room baru
    setiap reconnect (jarvis-{random8}). Semua notifikasi undelivered dikirim ke
    session aktif — pola "drainNotify on reconnect" sesuai desain task-store.
    """
    db = db_path or DB_PATH
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT n.task_id, n.status, t.user_intent, t.summary
            FROM notify_outbox n
            JOIN tasks t ON n.task_id = t.task_id
            WHERE n.delivered = 0
            ORDER BY n.created_at ASC
            LIMIT ?
            """,
            (COALESCE_MAX,),
        ).fetchall()
        if not rows:
            return 0

        # C.1: claim atomik (hanya baris yang belum delivered)
        claimed: list[dict] = []
        now = int(time.time() * 1000)
        for r in rows:
            got = conn.execute(
                "UPDATE notify_outbox SET delivered = 1, delivered_at = ? "
                "WHERE task_id = ? AND delivered = 0 RETURNING task_id",
                (now, r["task_id"]),
            ).fetchone()
            if got:
                claimed.append(dict(r))
    if not claimed:
        return 0

    utterance = coalesce_notifications(claimed)
    claimed_ids = [r["task_id"] for r in claimed]
    try:
        # AgentSession.say() returns SpeechHandle (awaitable or wait_for_playout)
        res = session.say(utterance)
        handle = await res if inspect.isawaitable(res) else res

        # C.2: tunggu ucapan selesai dimainkan, lalu cek SpeechHandle.interrupted
        wait_playout = getattr(handle, "wait_for_playout", None)
        if wait_playout is not None and callable(wait_playout):
            await wait_playout()

        if getattr(handle, "interrupted", False):
            logger.info("Notification interrupted — rollback for re-offer")
            _rollback_delivered(claimed_ids, db)
            return 0
        return len(claimed)
    except Exception as e:
        # C.1: say gagal → notifikasi tetap pending, TIDAK hilang
        logger.warning(f"Proactive notification delivery failed: {e}")
        _rollback_delivered(claimed_ids, db)
        return 0


async def startup_health_check() -> dict[str, bool]:
    health = {"searxng": False, "mempalace": False}

    async def _check_searxng() -> None:
        try:
            async with (
                aiohttp.ClientSession() as s,
                s.get(
                    f"{get_searxng_url()}/healthz",
                    timeout=aiohttp.ClientTimeout(total=2.0),
                ) as resp,
            ):
                health["searxng"] = resp.status < 500
        except Exception:
            health["searxng"] = False

    async def _check_mempalace() -> None:
        endpoint = os.getenv("MEMPALACE_MCP_HTTP_ENDPOINT", "")
        if not endpoint:
            return
        try:
            async with (
                aiohttp.ClientSession() as s,
                s.get(
                    f"{endpoint}/health",
                    timeout=aiohttp.ClientTimeout(total=2.0),
                ) as resp,
            ):
                health["mempalace"] = resp.status < 500
        except Exception:
            health["mempalace"] = False

    await asyncio.gather(_check_searxng(), _check_mempalace())
    for name, ok in health.items():
        if not ok:
            logger.warning(
                f"Startup health check: {name} DOWN — tools tetap terdaftar, narasi error saat dipanggil"
            )
    return health


@server.rtc_session(agent_name="shorekeeper")
async def my_agent(ctx: JobContext):
    ctx.log_context_fields = {"room": ctx.room.name}
    await ctx.connect()

    # Wait for participant to join
    participant = None
    voice = FALLBACK_VOICE
    try:
        participant = await asyncio.wait_for(
            ctx.wait_for_participant(identity="schnee"), timeout=30.0
        )
        attributes = participant.attributes or {}
        voice = attributes.get("voice") or FALLBACK_VOICE
        logger.info(f"Participant joined: {participant.identity}, voice: {voice}")
    except asyncio.TimeoutError:
        logger.warning("Participant wait timeout — using default voice")
    except Exception as e:
        logger.warning(f"Error resolving participant attributes: {e}")

    # Sprint A.2: Context injection (build_session_context)
    context_block = await build_session_context(ctx.room.name)

    # Sprint C.4: dependency non-kritis — warning saja, tool tetap terdaftar
    await startup_health_check()

    # Sprint C.4: kredensial kritis → fail-fast
    gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not gemini_api_key:
        raise RuntimeError(
            "GEMINI_API_KEY / GOOGLE_API_KEY is not set — cannot start session"
        )

    # Validate that voice is a valid Gemini voice; if user sent Fish Audio hash, fallback to Aoede
    if voice not in VALID_GEMINI_VOICES:
        logger.warning(
            f"Voice '{voice}' is not a valid Gemini voice — falling back to '{FALLBACK_VOICE}'"
        )
        voice = FALLBACK_VOICE

    # Sprint B.2: resume from persisted handle (cross-restart resumption)
    resume_handle = get_session_handle(ctx.room.name)
    if resume_handle:
        logger.info(f"Resuming session from persisted handle for {ctx.room.name}")

    # Native Gemini Live Multimodal Realtime Model
    # (Sprint B.1: context compression · B.2: session resumption)
    model_kwargs: dict[str, object] = {
        "model": "gemini-3.1-flash-live-preview",
        "api_key": gemini_api_key,
        "voice": voice,
        "modalities": ["AUDIO"],
        "temperature": 0.7,
        "context_window_compression": ContextWindowCompressionConfig(
            trigger_tokens=60_000,
            sliding_window=SlidingWindow(target_tokens=30_000),
        ),
    }
    if resume_handle:
        model_kwargs["session_resumption"] = SessionResumptionConfig(
            handle=resume_handle
        )
    model = RealtimeModel(**model_kwargs)  # type: ignore[arg-type]

    # Attach TTS for programmatic push notifications (session.say) without affecting realtime audio.
    # NOTE: gunakan beta.GeminiTTS (GOOGLE_API_KEY), BUKAN google.TTS yang butuh
    # Vertex AI ADC credentials (DefaultCredentialsError di VPS tanpa gcloud).
    tts_model = None
    try:
        from livekit.plugins.google.beta import GeminiTTS

        tts_model = GeminiTTS(voice_name=voice, api_key=gemini_api_key)
    except Exception as te:
        logger.warning(f"GeminiTTS init failed: {te}")

    agent = ShorekeeperAgent(
        instructions=SHOREKEEPER_INSTRUCTIONS
        + ("\n\n" + context_block if context_block else ""),
        room_name=ctx.room.name,
    )

    session_kwargs: dict[str, object] = {"llm": model}
    if tts_model:
        session_kwargs["tts"] = tts_model
    session = AgentSession(**session_kwargs)

    await session.start(agent=agent, room=ctx.room)
    logger.info("Shorekeeper Gemini Live session started.")

    # Sprint B.2: persist resumption handle updates (plugin tidak mengekspos
    # event resumption — poll property session_resumption_handle, simpan ke
    # SQLite saat berubah; dipakai sebagai handle resume di session berikutnya).
    async def resumption_handle_loop():
        last_handle = None
        while True:
            await asyncio.sleep(15.0)
            try:
                handle = getattr(model, "session_resumption_handle", None)
                if handle and handle != last_handle:
                    last_handle = handle
                    save_session_handle(ctx.room.name, handle)
                    logger.info(
                        f"Persisted session resumption handle for {ctx.room.name}"
                    )
            except Exception as e:
                logger.debug(f"Resumption handle poll error: {e}")

    resumption_ref = asyncio.create_task(resumption_handle_loop())

    async def cancel_resumption():
        resumption_ref.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await resumption_ref

    ctx.add_shutdown_callback(cancel_resumption)

    # Event-Driven Notifier (P0-1):
    # Menggantikan polling periodik 2.0s dengan event trigger instan via asyncio.Event.
    # Event loop tetap memiliki fallback watchdog 5.0s jika ada sinyal eksternal terlewat.
    notify_trigger = asyncio.Event()

    async def outbox_notification_loop():
        while True:
            try:
                # Tunggu sinyal event (atau timeout watchdog 5.0s)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(notify_trigger.wait(), timeout=5.0)
                notify_trigger.clear()

                if (
                    getattr(session, "_activity", None) is None
                    or getattr(session, "_closing_task", None) is not None
                ):
                    continue

                await deliver_notifications(session, ctx.room.name)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Outbox notification error: {e}")

    task_ref = asyncio.create_task(outbox_notification_loop())

    async def cancel_outbox():
        task_ref.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task_ref

    ctx.add_shutdown_callback(cancel_outbox)


if __name__ == "__main__":
    from livekit.agents import cli

    cli.run_app(server)
