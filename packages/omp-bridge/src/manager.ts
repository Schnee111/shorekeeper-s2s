/**
 * manager.ts — worker manager (TASK-2.2): spawn/kill/retry/timeout + heartbeat.
 *
 * Pool dengan HARD CAP max 3 worker paralel (riset konflik: cap adalah batas,
 * bukan target) + FIFO queue untuk sisanya. Semua state di task store
 * (single-writer = orchestrator/manager process; worker TIDAK menyentuh DB).
 *
 * Siklus hidup per task:
 *   queued → (slot kosong & ownership bebas) → running → worker-ok →
 *   onWorkerReady (orchestrator merge gate) → done | failed | blocked
 *   timeout → kill (bridge SIGKILL) → cek idempotensi (artifact+verifier) →
 *   retry eksponensial (1s/4s/16s, HANYA step idempoten) → failed/`TIMEOUT (N attempts)`
 *   kill gagal (zombie) → Pid dicatat, failed/`ZOMBIE_KILL_FAILED`, alert line,
 *   slot TIDAK pernah terblokir.
 *
 * Pre-spawn ownership (TASK-2.3): task dengan overlap file TIDAK di-spawn
 * paralel dengan owner yang SEDANG RUNNING — tetap `queued` (conflict-deferred)
 * sampai owner selesai & release; `force:true` + bentrok → ditolak
 * `CONFLICT_DETECTED` + daftar pemilik. Slot pool DITAHAN sampai merge gate
 * (onWorkerReady) selesai sehingga release ownership selalu terjadi SEBELUM
 * task ter-defer berikutnya jalan (tidak ada merge paralel antar owner bentrok).
 *
 * Heartbeat: touchHeartbeat ≤ 30s (interval default 30_000) oleh manager
 * (bukan worker); saat manager restart → recoverStale() → stale running
 * menjadi failed/STALE_HEARTBEAT (store.staleTasks — pakai, jangan buat ulang).
 *
 * Idempotency: sebelum re-dispatch cek artifact_dir/diff — jika task SUDAH
 * landing (file berubah + test hijau di main repo) → done TANPA re-run
 * (spawn count tidak bertambah).
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import type { TaskSpec } from "handoff-contract";
import { initObservability, setPoolSizeProvider, type ObservabilityHandle } from "shorekeeper-observability";
import { removeWorktree, runTask, type RunTaskResult } from "./index.js";
import { safetyAlertLine, scanSpecForbidden, specTexts } from "./safety.js";
import { TaskStore, type TransitionMeta } from "task-store";

export const MANAGER_VERSION = "0.1.0";
export const MAX_PARALLEL_HARD_CAP = 3; // riset conflict rate antar-agent — batas, bukan target
export const HEARTBEAT_MAX_INTERVAL_MS = 30_000;
export const DEFAULT_STALE_TTL_SECONDS = 60;

/**
 * Ownership kuadrat (TASK-2.3) — struktural, tanpa import keras dari conflict-map.
 * Kontrak: return task lain yang SEDANG RUNNING dan overlap file — hanya owner
 * aktif (running) yang boleh men-defer spawn; task queued tidak saling blokir
 * (FIFO men-serialisasi dispatch mereka).
 */
export interface OwnershipLike {
  conflictsWith(taskId: string): string[];
}

export interface WorkerReadyInfo {
  /** Branch worker di repo utama: `worker/<taskId>` (dibuat manager, bukan worker process). */
  branch: string;
  branchSha: string | null;
  exitCode: number;
  diffSummary: string;
}

export interface WorkerManagerEvent {
  type:
    | "queued"
    | "dispatch"
    | "worker-ok"
    | "timeout"
    | "spawn-error"
    | "retry"
    | "idempotent-done"
    | "conflict-deferred"
    | "conflict-rejected"
    | "alert"
    | "zombie"
    | "terminal"
    | "recovered-stale"
    | "heartbeat"
    | "slot-freed";
  taskId: string;
  attempt?: number;
  spawnSeq?: number;
  backoffMs?: number;
  reason?: string;
  status?: string;
  error?: string;
  owners?: string[];
  pid?: number | null;
  message?: string;
}

export interface RunnerOptions {
  allowlist: string[];
  timeoutMs: number;
  keepWorktree: boolean;
  mock: boolean;
  mockCommand?: string[];
  worktreeBase: string;
  env: Record<string, string>;
}

export type RunnerImpl = (spec: TaskSpec, repoPath: string, opts: RunnerOptions) => Promise<RunTaskResult>;

export interface SpawnTaskOptions {
  spec: TaskSpec;
  timeoutMs?: number;
  maxRetries?: number;
  mock?: boolean;
  /** Env tambahan proses worker (mis. OMP_BRIDGE_MOCK_SLEEP_MS untuk uji timeout). */
  env?: Record<string, string>;
  /** User memaksa spawn meski bentrok → ditolak CONFLICT_DETECTED (TASK-2.3). */
  force?: boolean;
}

export interface SpawnResult {
  taskId: string;
  status: "running" | "queued" | "rejected" | "terminal";
  reason?: string;
  owners?: string[];
}

export interface WorkerManagerOptions {
  store: TaskStore;
  /** Allowlist repo (path absolut). Wajib — default deny. */
  allowlist: string[];
  worktreeBase?: string;
  /** HARD CAP 3 (di-clamp; > 3 tidak pernah). */
  maxParallel?: number;
  /** Interval heartbeat ms — WAJIB ≤ 30_000. */
  heartbeatIntervalMs?: number;
  /** TTL stale (detik) untuk recoverStale saat start. */
  staleTtlSeconds?: number;
  /** Retry maks untuk step idempoten (default 2 → total 3 attempts). */
  maxRetries?: number;
  /** Backoff eksponensial per retry (default [1000, 4000]). */
  retryBackoffMs?: number[];
  /** Timeout per attempt (default 300_000). */
  defaultTimeoutMs?: number;
  /** Base dir artifact (default data/artifacts). */
  artifactDirBase?: string;
  /** Ownership map (TASK-2.3): pre-spawn check — overlap tetap queued. */
  ownership?: OwnershipLike | null;
  /** Verifier (test suite repo) untuk cek idempotensi. */
  verifierCmd?: string | string[];
  /** Dir untuk marker spawn counter (tests/E2E: data/spawns). */
  mockMarkerDir?: string;
  /** Runner injectable (unit test; default bridge runTask MOCK). */
  runner?: RunnerImpl | null;
  /** false = kill gagal (zombie) → failed + alert, slot tetap bebas. */
  killWorker?: (taskId: string, pid: number | null | undefined) => boolean;
  mockCommand?: string[];
  sleepMs?: (ms: number) => Promise<void>;
  onEvent?: (evt: WorkerManagerEvent) => void;
  /** Worker selesai (branch worker siap) → orchestrator merge gate. */
  onWorkerReady?: (taskId: string, info: WorkerReadyInfo) => Promise<void> | void;
  /** Task mencapai status terminal (done/failed/blocked) — release ownership dst. */
  onTaskTerminal?: (taskId: string, status: string) => Promise<void> | void;
}

interface Slot {
  attempt: number;
  pid: number | null;
}

const delay = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

export function parseVerifierCmd(cmd: string | string[]): { file: string; args: string[] } {
  if (Array.isArray(cmd)) {
    if (cmd.length === 0) throw new Error("WorkerManager: verifierCmd cannot be empty array");
    return { file: cmd[0], args: cmd.slice(1) };
  }
  const trimmed = cmd.trim();
  if (!trimmed) throw new Error("WorkerManager: verifierCmd cannot be empty string");

  const matches = trimmed.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) || [];
  const parsed = matches.map((m) => {
    if ((m.startsWith('"') && m.endsWith('"')) || (m.startsWith("'") && m.endsWith("'"))) {
      return m.slice(1, -1);
    }
    return m;
  });
  const file = parsed[0];
  if (!file) throw new Error("WorkerManager: executable not found in verifierCmd");
  return { file, args: parsed.slice(1) };
}

export class WorkerManager {
  private opts: {
    worktreeBase: string;
    maxParallel: number;
    heartbeatIntervalMs: number;
    staleTtlSeconds: number;
    maxRetries: number;
    retryBackoffMs: number[];
    defaultTimeoutMs: number;
    artifactDirBase: string;
    ownership: OwnershipLike | null;
    verifierCmd: string | string[];
    mockMarkerDir: string;
    runner: RunnerImpl;
    killWorker: (taskId: string, pid: number | null | undefined) => boolean;
    mockCommand?: string[];
    sleepMs: (ms: number) => Promise<void>;
    onEvent: (evt: WorkerManagerEvent) => void;
    onWorkerReady?: WorkerManagerOptions["onWorkerReady"];
    onTaskTerminal?: WorkerManagerOptions["onTaskTerminal"];
    store: TaskStore;
    allowlist: string[];
  };
  private slots = new Map<string, Slot>();
  private fifo: string[] = [];
  private specs = new Map<string, TaskSpec>();
  private repoOf = new Map<string, string>();
  private spawnOpts = new Map<string, SpawnTaskOptions>();
  private lastPid = new Map<string, number | null>();
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private pending = new Set<Promise<unknown>>();
  /** Task yang sudah diberi event conflict-deferred (hindari spam per pump). */
  private deferredNotified = new Set<string>();
  private _spawnCount = 0;
  /** Attempt terakhir per task (untuk attribute retry_count di span task.run). */
  private attemptsByTask = new Map<string, number>();
  /** TASK-3.1: handle OTel (null hanya bila init gagal — fail-open). */
  private otel: ObservabilityHandle | null = null;

  constructor(opts: WorkerManagerOptions) {
    if (!opts.store) throw new Error("WorkerManager: store wajib diisi");
    if (!opts.allowlist || opts.allowlist.length === 0) {
      throw new Error("WorkerManager: allowlist wajib non-kosong (default deny)");
    }
    const maxParallel = Math.min(MAX_PARALLEL_HARD_CAP, opts.maxParallel ?? MAX_PARALLEL_HARD_CAP);
    const heartbeatIntervalMs = Math.min(HEARTBEAT_MAX_INTERVAL_MS, opts.heartbeatIntervalMs ?? HEARTBEAT_MAX_INTERVAL_MS);
    this.opts = {
      store: opts.store,
      allowlist: opts.allowlist,
      worktreeBase: opts.worktreeBase ?? join(tmpdir(), "sk-omp"),
      maxParallel,
      heartbeatIntervalMs,
      staleTtlSeconds: opts.staleTtlSeconds ?? DEFAULT_STALE_TTL_SECONDS,
      maxRetries: opts.maxRetries ?? 2,
      retryBackoffMs: opts.retryBackoffMs ?? [1000, 4000],
      defaultTimeoutMs: opts.defaultTimeoutMs ?? 300_000,
      artifactDirBase: opts.artifactDirBase ?? "data/artifacts",
      ownership: opts.ownership ?? null,
      verifierCmd: opts.verifierCmd ?? "",
      mockMarkerDir: opts.mockMarkerDir ?? "",
      runner: opts.runner ?? ((spec, repoPath, rOpts) => runTask(spec, repoPath, rOpts)),
      killWorker: opts.killWorker ?? (() => true),
      mockCommand: opts.mockCommand,
      sleepMs: opts.sleepMs ?? delay,
      onEvent: opts.onEvent ?? (() => {}),
      onWorkerReady: opts.onWorkerReady,
      onTaskTerminal: opts.onTaskTerminal,
    };
    // TASK-3.1: observability (idempotent per proses; fail-open bila kolektor mati).
    try {
      this.otel = initObservability();
      setPoolSizeProvider(() => this.runningCount());
    } catch {
      // tracing tidak boleh menghentikan orkestrasi — lanjut tanpa instrumen
      this.otel = null;
    }
  }

  // -- introspection (tests & E2E) -------------------------------------------

  get spawnCount(): number {
    return this._spawnCount;
  }

  runningCount(): number {
    return this.slots.size;
  }

  queuedCount(): number {
    return this.fifo.length;
  }

  peekQueued(): string[] {
    return [...this.fifo];
  }

  // -- lifecycle ---------------------------------------------------------------

  /**
   * Start manager: recoverStale (running basi → failed/STALE_HEARTBEAT) +
   * mulai heartbeat interval (≤ 30s, single-writer).
   */
  async start(): Promise<{ recovered: string[] }> {
    const stale = this.opts.store.staleTasks(this.opts.staleTtlSeconds);
    for (const rec of stale) {
      this.opts.onEvent({ type: "recovered-stale", taskId: rec.task_id, error: rec.error ?? "STALE_HEARTBEAT" });
    }
    if (!this.heartbeatTimer) {
      this.heartbeatTimer = setInterval(() => this.tickHeartbeat(), this.opts.heartbeatIntervalMs);
      this.heartbeatTimer.unref?.();
    }
    return { recovered: stale.map((r) => r.task_id) };
  }

  stop(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  /** Strategi restart & reconciliation (Fase 7 / P1): tandai gantung sebagai unknown untuk audit/reconcile aman */
  async recoverStale(): Promise<string[]> {
    const stale = this.opts.store.staleTasks(this.opts.staleTtlSeconds);
    for (const rec of stale) {
      // Transition to 'unknown' to fence ambiguous crash outcomes
      try {
        this.opts.store.transition(rec.task_id, "unknown", {
          error: "CRASH_RECONCILED: task gantung saat restart / heartbeat basi",
        });
      } catch {
        /* sudah terminal */
      }
      this.opts.onEvent({ type: "recovered-stale", taskId: rec.task_id, error: "CRASH_RECONCILED" });
    }
    return stale.map((r) => r.task_id);
  }

  /**
   * Enqueue task (FIFO): antre bila pool penuh / ownership bentrok; task
   * berikutnya berjalan saat slot kosong. `force:true` + bentrok →
   * ditolak `CONFLICT_DETECTED` + daftar pemilik (TASK-2.3).
   */
  async spawnTask(taskId: string, repoPath: string, o: SpawnTaskOptions): Promise<SpawnResult> {
    const rec = this.opts.store.getTask(taskId);
    if (!rec) throw new Error(`WorkerManager: task ${taskId} tidak ada di store (seed dulu)`);

    if (rec.status === "done" || rec.status === "failed" || rec.status === "cancelled") {
      return { taskId, status: "terminal", reason: `status=${rec.status}` };
    }
    if (this.slots.has(taskId)) return { taskId, status: "running" };

    // TASK-3.2 (injection): scan path terlarang SEBELUM simpan spec/claim —
    // ditolak REPO_NOT_ALLOWED + alert, TANPA spawn (spawn counter 0).
    // Task di-`cancelled` (terminal): spec berbahaya tidak boleh masuk antrian.
    const violation = scanSpecForbidden(specTexts(o.spec));
    if (violation) {
      this.opts.onEvent({
        type: "conflict-rejected",
        taskId,
        reason: violation.code,
        message: safetyAlertLine(taskId, violation),
      });
      try {
        this.opts.store.transition(taskId, "cancelled", { error: `${violation.code}: ${violation.reason} [${violation.matched.join(";")}]` });
      } catch {
        // sudah terminal — abaikan
      }
      return { taskId, status: "rejected", reason: violation.code, owners: [] };
    }

    // TASK-3.1: root span task.run + metric task_created (metadata only — privasi).
    if (this.otel) {
      this.otel.tracer.taskStart(taskId, { lane: rec.lane, status: rec.status });
      this.otel.metrics.taskCreatedInc(taskId, rec.lane);
    }

    // Simpan spec/repo/opts SEBELUM cek idempotensi & ownership agar checkLanded
    // dan dispatch dapat repoPath yang konsisten (pakai, jangan buat ulang state).
    this.specs.set(taskId, o.spec);
    this.repoOf.set(taskId, repoPath);
    this.spawnOpts.set(taskId, o);

    // force + bentrok → TOLAK (CONFLICT_DETECTED) — dicek SEBELUM fifo.includes
    // agar re-spawn yang dipaksa tetap dapat bukti konflik, bukan "queued".
    if (o.force) {
      const owners = this.owners(taskId);
      if (owners.length > 0) {
        this.opts.onEvent({ type: "conflict-rejected", taskId, owners, reason: "CONFLICT_DETECTED" });
        return { taskId, status: "rejected", reason: "CONFLICT_DETECTED", owners };
      }
    }
    if (this.fifo.includes(taskId)) return { taskId, status: "queued" };

    // Recovery (report hilang): cek idempotensi SEBELUM re-dispatch — task yang
    // sudah landing → done TANPA re-run (spawn count tidak bertambah).
    if (rec.status === "running" || rec.status === "blocked") {
      if (await this.checkLanded(taskId)) {
        this.opts.onEvent({ type: "idempotent-done", taskId, reason: "diff landed + verifier hijau" });
        await this.terminal(taskId, "done", "idempotent: work landed sebelum report hilang");
        return { taskId, status: "terminal", reason: "idempotent: work landed sebelum report hilang" };
      }
    }

    this.fifo.push(taskId);
    this.opts.onEvent({ type: "queued", taskId });
    await this.pump();
    // Beri kesempatan dispatch idempoten (checkLanded → terminal) selesai
    // sebelum spawnTask return, agar caller melihat status final.
    await this.opts.sleepMs(0);
    const after = this.opts.store.getTask(taskId);
    if (after && (after.status === "done" || after.status === "failed" || after.status === "cancelled")) {
      return { taskId, status: "terminal", reason: `status=${after.status}` };
    }
    return { taskId, status: this.slots.has(taskId) ? "running" : "queued" };
  }

  /** Dipanggil driver setelah ownership di-release (owner selesai) → coba jalan lagi. */
  notifyReleased(): void {
    void this.pump();
  }

  /** Tunggu SEMUA task tuntas (queue kosong, slot kosong, callback merge selesai). */
  async drain(): Promise<void> {
    while (this.fifo.length > 0 || this.slots.size > 0 || this.pending.size > 0) {
      await this.opts.sleepMs(50);
    }
  }

  // -- internals ---------------------------------------------------------------

  /** Owner aktif = task yang SEDANG RUNNING di slot pool & overlap file.
   *  Task yang hanya queued/blocked belum mengklaim file secara aktif —
   *  FIFO pump akan menserialize dispatch mereka. Hanya owner RUNNING yang
   *  boleh men-defer (task bentrok tetap antre sampai owner selesai). */
  private owners(taskId: string): string[] {
    if (!this.opts.ownership) return [];
    const seen = this.opts.ownership.conflictsWith(taskId);
    // hanya yang memang sedang dijalankan di pool ini (slot aktif)
    return seen.filter((other) => this.slots.has(other));
  }

  private track(p: Promise<unknown>): void {
    this.pending.add(p);
    void p.finally(() => this.pending.delete(p));
  }

  /**
   * Isi slot kosong dari FIFO (skip-scan). Task yang bentrok ownership (owner
   * sedang RUNNING) dilewati dan tetap antre (conflict-deferred, event sekali
   * per periode deferral) — pump ulang via notifyReleased setelah owner release.
   * Skip-scan mencegah head-of-line blocking: task independen di belakang task
   * bentrok tetap bisa jalan. Slot di-set SEBELUM dispatch (hard cap pool
   * tidak pernah lewat).
   */
  private async pump(): Promise<void> {
    let progress = true;
    while (progress && this.slots.size < this.opts.maxParallel && this.fifo.length > 0) {
      progress = false;
      for (let i = 0; i < this.fifo.length; i++) {
        if (this.slots.size >= this.opts.maxParallel) break;
        const taskId = this.fifo[i]!;
        const owners = this.owners(taskId);
        if (owners.length > 0) {
          if (!this.deferredNotified.has(taskId)) {
            this.deferredNotified.add(taskId);
            this.opts.onEvent({ type: "conflict-deferred", taskId, owners });
          }
          continue; // tetap antre sampai owner selesai (pump ulang via notifyReleased)
        }
        this.fifo.splice(i, 1);
        this.deferredNotified.delete(taskId);
        this.slots.set(taskId, { attempt: 0, pid: null });
        this.dispatch(taskId);
        progress = true;
        break; // fifo berubah — ulangi scan dari depan
      }
    }
  }

  private dispatch(taskId: string): void {
    const p = this.runAttemptLoop(taskId).finally(() => {
      this.slots.delete(taskId);
      this.opts.onEvent({ type: "slot-freed", taskId, reason: `${this.slots.size} running` });
      return this.pump();
    });
    this.track(p);
  }

  /**
   * Loop attempts: worker-ok exit 0 → merge gate (onWorkerReady, slot ditahan);
   * exit ≠ 0 (test merah) → failed/VERIFY_FAILED TANPA retry (deterministik,
   * non-idempoten); error bridge (TIMEOUT/SPAWN_ERROR) → cek idempotensi →
   * retry backoff → failed/<CODE> (N attempts). Zombie → failed + alert, slot bebas.
   */
  private async runAttemptLoop(taskId: string): Promise<void> {
    const o = this.spawnOpts.get(taskId)!;
    const repoPath = this.repoOf.get(taskId)!;
    const spec = this.specs.get(taskId)!;
    const maxAttempts = Math.max(1, (o.maxRetries ?? this.opts.maxRetries) + 1);

    // status store: queued|blocked → running (single-writer manager)
    const before = this.opts.store.getTask(taskId);
    if (before && (before.status === "queued" || before.status === "blocked")) {
      try {
        this.opts.store.transition(taskId, "running", { worker_pid: process.pid });
      } catch {
        // sudah running (race/retry) — lanjut
      }
    }

    // IDEMPOTENSI (masuk): task sudah landing (diff + verifier hijau) → done TANPA re-run
    if (await this.checkLanded(taskId)) {
      this.opts.onEvent({ type: "idempotent-done", taskId, reason: "diff landed + verifier hijau" });
      await this.terminal(taskId, "done", "idempotent: work landed sebelum report hilang");
      return;
    }

    try {
      for (let attempt = 1; attempt <= maxAttempts; attempt++) {
        const slot = this.slots.get(taskId) ?? { attempt: 0, pid: null };
        slot.attempt = attempt;
        this.slots.set(taskId, slot);
        this.attemptsByTask.set(taskId, attempt);

        this._spawnCount += 1;
        this.opts.onEvent({ type: "dispatch", taskId, attempt, spawnSeq: this._spawnCount });
        const marker = this.writeMarker(taskId, attempt);

        // TASK-3.1: span delegate_task (enqueue→ack) + worker.run (durasi worker).
        const t0 = Date.now();
        if (this.otel) {
          this.otel.tracer.childStart(taskId, "delegate_task", { attempt, spawn_seq: this._spawnCount });
          this.otel.tracer.childStart(taskId, "worker.run", { attempt });
        }

        let result: RunTaskResult;
        try {
          result = await this.opts.runner(spec, repoPath, {
            allowlist: this.opts.allowlist,
            timeoutMs: o.timeoutMs ?? this.opts.defaultTimeoutMs,
            keepWorktree: true,
            mock: o.mock ?? process.env.OMP_BRIDGE_MOCK === "1",
            mockCommand: this.opts.mockCommand,
            worktreeBase: this.opts.worktreeBase,
            env: { OMP_BRIDGE_MOCK_MARKER: marker, ...(o.env ?? {}) },
          });
        } catch (err) {
          result = {
            status: "error",
            code: "SPAWN_ERROR",
            message: `runner throw: ${err instanceof Error ? err.message : String(err)}`,
          };
        }
        this.lastPid.set(taskId, result.pid ?? null);

        // Tutup span delegate_task + worker.run + metric durasi (metadata only).
        if (this.otel) {
          const durSec = (Date.now() - t0) / 1000;
          const errMarker = result.status === "error" ? { code: result.code } : undefined;
          this.otel.tracer.childEnd(
            taskId,
            "delegate_task",
            { worker_pid: result.pid ?? null, delegate_ms: Math.round(durSec * 1000), status: result.status },
            errMarker,
          );
          this.otel.tracer.childEnd(
            taskId,
            "worker.run",
            { worker_pid: result.pid ?? null, attempt, status: result.status, exit_code: result.status === "ok" ? result.exitCode : null },
            errMarker,
          );
          this.otel.metrics.workerDurationSeconds(durSec, taskId, spec.lane);
        }

        if (result.status === "ok" && result.exitCode === 0) {
          await this.handleWorkerOk(taskId, attempt, result);
          // Merge gate (onWorkerReady) sudah selesai → pastikan onTaskTerminal
          // (release ownership) dan event terminal fire. terminal() toleran
          // terhadap transisi yang sudah dilakukan oleh driver di onWorkerReady
          // (done→done → INVALID_TRANSITION, ditangkap, tetap panggil onTaskTerminal).
          try { await this.terminal(taskId, "done", "worker-ok merged"); } catch { /* terminal swallow */ }
          return;
        }
        if (result.status === "ok") {
          // worker selesai tapi test merah — deterministik, non-idempoten → quarantine
          await this.storeArtifacts(taskId, result);
          return this.terminal(taskId, "failed", "VERIFY_FAILED");
        }

        // error bridge: TIMEOUT / SPAWN_ERROR / dst
        this.opts.onEvent({
          type: result.code === "TIMEOUT" ? "timeout" : "spawn-error",
          taskId,
          attempt,
          reason: `${result.code}: ${result.message.slice(0, 120)}`,
        });

        // idempotensi: task SUDAH landing (file berubah + verifier hijau) → done tanpa re-run
        if (result.code === "TIMEOUT" && (await this.checkLanded(taskId))) {
          this.opts.onEvent({ type: "idempotent-done", taskId, reason: "diff landed + verifier hijau" });
          return this.terminal(taskId, "done", "idempotent: work landed sebelum report hilang");
        }

        // zombie: kill gagal → Pid dicatat, failed + alert, slot TIDAK terblokir
        if (result.code === "TIMEOUT" && !this.opts.killWorker(taskId, result.pid)) {
          const pid = result.pid ?? this.lastPid.get(taskId) ?? null;
          this.opts.onEvent({
            type: "zombie",
            taskId,
            pid,
            message: `kill gagal — proses worker masih hidup (zombie)`,
          });
          this.opts.onEvent({
            type: "alert",
            taskId,
            message: `zombie-worker task=${taskId} pid=${pid ?? "unknown"} kill-failed — slot dibebaskan, jangan blokir pool`,
          });
          return this.terminal(taskId, "failed", `ZOMBIE_KILL_FAILED (pid ${pid ?? "unknown"})`, pid);
        }

        if (attempt < maxAttempts) {
          const backoffMs = this.opts.retryBackoffMs[attempt - 1] ?? 1000;
          this.opts.onEvent({ type: "retry", taskId, attempt, backoffMs, reason: result.code });
          if (this.otel) this.otel.metrics.taskRetriedInc(taskId, spec.lane);
          await this.opts.sleepMs(backoffMs);
          continue;
        }
        return this.terminal(taskId, "failed", `${result.code} (${maxAttempts} attempts)`);
      }
    } finally {
      this.slots.delete(taskId);
    }
  }

  /** Worker selesai exit 0: commit worktree → branch worker/<taskId>, artifact, cleanup. */
  private async handleWorkerOk(taskId: string, attempt: number, result: RunTaskResult): Promise<void> {
    if (result.status !== "ok") return;
    const repoPath = this.repoOf.get(taskId)!;
    let branchSha: string | null = null;
    const hasChanges = (result.diffFull ?? "").trim().length > 0;
    if (hasChanges && result.worktree) {
      try {
        execFileSync("git", ["-C", result.worktree, "add", "-A"], { stdio: "pipe" });
        execFileSync(
          "git",
          [
            "-C",
            result.worktree,
            "-c",
            "user.name=Shorekeeper Worker",
            "-c",
            "user.email=worker@shorekeeper.local",
            "commit",
            "-qm",
            `worker(${attempt}): ${taskId}`,
          ],
          { stdio: "pipe" },
        );
        const sha = execFileSync("git", ["-C", result.worktree, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
        // branch dibuat di REPO UTAMA oleh MANAGER (bukan worker process) — orchestrator merge gate
        execFileSync("git", ["-C", repoPath, "branch", "-f", `worker/${taskId}`, sha], { stdio: "pipe" });
        branchSha = sha;
      } catch (err) {
        await this.storeArtifacts(taskId, result);
        return this.terminal(taskId, "failed", `WORKTREE_COMMIT_FAILED: ${err instanceof Error ? err.message.slice(0, 200) : String(err)}`);
      }
    }

    await this.storeArtifacts(taskId, result);
    if (result.worktree) removeWorktree(repoPath, result.worktree);

    this.opts.onEvent({ type: "worker-ok", taskId, attempt, reason: hasChanges ? "diff landed" : "no changes" });
    // Slot DITAHAN sampai merge gate (onWorkerReady) selesai: release ownership
    // terjadi di callback merge, sehingga task bentrok yang ter-defer baru bisa
    // jalan SETELAH owner benar-benar selesai + ter-merge (TASK-2.3). Slot bebas
    // di finally runAttemptLoop (durasi pool = proses worker + merge gate).
    try {
      await this.opts.onWorkerReady?.(taskId, {
        branch: `worker/${taskId}`,
        branchSha,
        exitCode: result.exitCode,
        diffSummary: result.diffSummary,
      });
    } catch (err) {
      this.opts.onEvent({
        type: "alert",
        taskId,
        message: `onWorkerReady gagal: ${err instanceof Error ? err.message : String(err)}`,
      });
    }
  }

  /** Artifact ke filesystem (DB hanya path — kontrak Fase 1). */
  private async storeArtifacts(taskId: string, result: RunTaskResult): Promise<void> {
    const base = resolve(this.opts.artifactDirBase);
    try {
      this.opts.store.storeArtifactContent(taskId, base, "diff.patch", (result as { diffFull?: string }).diffFull ?? "");
      this.opts.store.storeArtifactContent(taskId, base, "diff-stat.txt", (result as { diffSummary?: string }).diffSummary ?? "");
      const tail = (result as { stdoutTail?: string }).stdoutTail;
      if (tail) this.opts.store.storeArtifactContent(taskId, base, "out.log", tail);
    } catch {
      // best-effort — artifact tidak boleh menggagalkan status task
    }
  }

  private async terminal(
    taskId: string,
    status: "done" | "failed" | "blocked",
    error: string,
    pid?: number | null,
  ): Promise<void> {
    const meta: TransitionMeta = { error };
    if (pid !== undefined && pid !== null) meta.worker_pid = pid;
    try {
      this.opts.store.transition(taskId, status, meta);
    } catch (err) {
      this.opts.onEvent({
        type: "alert",
        taskId,
        message: `transition ${status} gagal: ${err instanceof Error ? err.message : String(err)}`,
      });
    }
    // TASK-3.1: tutup root span task.run + metric terminal (metadata only).
    if (this.otel) {
      const rec = this.opts.store.getTask(taskId);
      const lane = rec?.lane;
      const retryCount = Math.max(0, (this.attemptsByTask.get(taskId) ?? 1) - 1);
      this.otel.tracer.taskEnd(taskId, { status, retry_count: retryCount }, status === "done" ? undefined : { code: error.split(" ")[0] ?? status, message: error.slice(0, 200) });
      if (status === "done") this.otel.metrics.taskDoneInc(taskId, lane);
      else if (status === "failed") this.otel.metrics.taskFailedInc(taskId, lane);
    }
    this.opts.onEvent({ type: "terminal", taskId, status, error });
    const p = Promise.resolve(this.opts.onTaskTerminal?.(taskId, status))
      .catch(() => undefined)
      .then(() => {
        // ownership mungkin di-release di onTaskTerminal → coba jalankan antrean
        this.notifyReleased();
      });
    this.track(p);
  }

  /** Idempotency: artifact diff ADA + non-kosong + verifier main repo HIJAU. */
  private async checkLanded(taskId: string): Promise<boolean> {
    const rec = this.opts.store.getTask(taskId);
    if (!rec?.artifact_dir) return false;
    const patch = join(rec.artifact_dir, "diff.patch");
    if (!existsSync(patch)) return false;
    let content = "";
    try {
      content = readFileSync(patch, "utf8");
    } catch {
      return false;
    }
    if (!content.trim() || !content.includes("diff --git")) return false; // file TIDAK berubah
    if (!this.opts.verifierCmd) return false;
    const repoPath = this.repoOf.get(taskId);
    if (!repoPath) return false;
    try {
      const { file, args } = parseVerifierCmd(this.opts.verifierCmd);
      execFileSync(file, args, {
        cwd: repoPath,
        encoding: "utf8",
        env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTEST_DISABLE_PLUGIN_AUTOLOAD: "1" },
      });
      return true;
    } catch {
      return false;
    }
  }

  private writeMarker(taskId: string, attempt: number): string {
    if (!this.opts.mockMarkerDir) return "";
    const dir = resolve(this.opts.mockMarkerDir);
    try {
      mkdirSync(dir, { recursive: true });
      const marker = join(dir, `${taskId}-a${attempt}.marker`);
      writeFileSync(marker, JSON.stringify({ taskId, attempt, at: Date.now() }));
      return marker;
    } catch {
      return "";
    }
  }

  private tickHeartbeat(): void {
    for (const taskId of this.slots.keys()) {
      const r = this.opts.store.touchHeartbeat(taskId);
      if (r.ok) this.opts.onEvent({ type: "heartbeat", taskId });
    }
  }
}
