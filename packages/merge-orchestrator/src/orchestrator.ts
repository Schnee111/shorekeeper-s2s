/**
 * orchestrator.ts — merge orchestrator (TASK-2.1): PEMEGANG TUNGGAL merge gate.
 *
 * Worker TIDAK pernah push/commit ke main (hard prohibition). Alur per task:
 *   kumpulkan artifact (data/artifacts/<task_id>/) → verifier read-only pada
 *   branch worker (test suite repo) → squash merge sequential ke main →
 *   task `done` + merge_commit (sha ≥ 7 char) tercatat (artifact merge.json +
 *   summary store) → gate approval push remote (default: main-local saja).
 *
 * Aturan kunci:
 * - Verifier MERAH → merge DITOLAK, task kembali `blocked` dengan
 *   error="VERIFY_FAILED" — tidak pernah force-merge, tidak `--no-verify`.
 * - Sequential queue: mergeTask di-rantai (satu-per-satu) — dua task yang
 *   menyentuh file sama tidak pernah di-merge paralel (TASK-2.3 pre-check
 *   merge-tree → overlap residual di-merge file-per-file oleh orchestrator).
 * - Approval: push `main` ke remote HANYA jika `approvalGranted`
 *   (env/CLI). Tanpa approval → branch lokal `main-local` saja.
 *   Push ditolak → retry 3× backoff → `failed` + instruksi manual.
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { mergeTreeOverlap, revParse } from "conflict-map";
import { getObservability } from "shorekeeper-observability";
import { TaskStore } from "task-store";

export const ORCHESTRATOR_VERSION = "0.1.0";
export const WORKER_BRANCH_PREFIX = "worker/";

export type MergeStatus =
  | "merged" // squash sukses → done + merge_commit
  | "empty" // tidak ada perubahan worker → done tanpa commit baru (verifier hijau)
  | "rejected" // verifier merah → blocked + error=VERIFY_FAILED
  | "push_rejected" // merge sukses lokal, push remote gagal setelah retry → failed
  | "merge_conflict" // squash bertabrakan → abort + blocked (resolusi manual)
  | "blocked_gate" // main tidak bersih / prasyarat gagal → blocked
  | "not_found" // task tidak ada di store
  | "bad_state"; // status task bukan running/blocked

export interface MergeTaskResult {
  status: MergeStatus;
  taskId: string;
  mergeCommit?: string;
  reason?: string;
}

export type OrchestratorEventType =
  | "merge-start"
  | "verify"
  | "overlap"
  | "merged"
  | "empty-merged"
  | "merge-rejected"
  | "merge-conflict"
  | "push"
  | "main-local"
  | "done";

export interface OrchestratorEvent {
  type: OrchestratorEventType;
  taskId: string;
  detail?: string;
  ok?: boolean;
}

export type VerifierCommand = string | string[];

export interface OrchestratorOptions {
  store: TaskStore;
  /** Test suite repo (verifier read-only). Wajib diisi. */
  verifierCmd: VerifierCommand;
  /** Base dir artifact (default data/artifacts). */
  artifactDirBase?: string;
  /** Base dir worktree sementara verifier (default os.tmpdir()/sk-ormerge). */
  worktreeBase?: string;
  /** Approval push remote (env/CLI). Default false = main-local saja. */
  approvalGranted?: boolean;
  /** URL remote; jika di-set maka origin dijamin menunjuk URL ini. */
  remoteUrl?: string;
  /** Nama remote (default origin). */
  remoteName?: string;
  /** Jumlah retry push maks (default 3). */
  pushRetries?: number;
  /** Backoff retry push (default [1000,4000,16000]). */
  pushBackoffMs?: number[];
  /** Identitas commit squash. */
  committerName?: string;
  committerEmail?: string;
  sleepMs?: (ms: number) => Promise<void>;
  onEvent?: (evt: OrchestratorEvent) => void;
  /**
   * Task ditutup oleh merge gate — hook lifecycle (TASK-2.3): release ownership
   * pada done/failed (task blocked menahan klaimnya — masih bisa di-retry),
   * lalu pump ulang antrean worker yang ter-defer. Dipanggil SEBELUM transisi
   * store pada sukses merge (release terjadi saat perubahan ter-merge).
   */
  onTaskClosed?: (taskId: string, status: "done" | "failed" | "blocked") => void;
  now?: () => number;
}

const delay = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

function runGit(repo: string, args: string[]): string {
  return execFileSync("git", ["-C", repo, ...args], { encoding: "utf8", maxBuffer: 16 * 1024 * 1024 });
}

function runGitSafe(repo: string, args: string[]): { stdout: string; exitCode: number } {
  try {
    return { stdout: runGit(repo, args), exitCode: 0 };
  } catch (err) {
    const e = err as { status?: number; stdout?: Buffer | string };
    return { stdout: String(e.stdout ?? ""), exitCode: e.status ?? 1 };
  }
}

export function parseVerifierCommand(cmd: VerifierCommand): { file: string; args: string[] } {
  if (Array.isArray(cmd)) {
    if (cmd.length === 0 || !cmd[0]) throw new Error("MergeOrchestrator: verifierCmd tidak boleh array kosong");
    return { file: cmd[0], args: cmd.slice(1) };
  }
  const trimmed = cmd.trim();
  if (!trimmed) throw new Error("MergeOrchestrator: verifierCmd wajib diisi (test suite repo)");

  // Safe argument splitting respecting quoted strings (no subshell invocation)
  const matches = trimmed.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) || [];
  const parsed = matches.map((m) => {
    if ((m.startsWith('"') && m.endsWith('"')) || (m.startsWith("'") && m.endsWith("'"))) {
      return m.slice(1, -1);
    }
    return m;
  });
  const file = parsed[0];
  if (!file) throw new Error("MergeOrchestrator: executable tidak ditemukan dalam verifierCmd");
  return { file, args: parsed.slice(1) };
}

export class MergeOrchestrator {
  private opts: Required<OrchestratorOptions>;
  private chain: Promise<void> = Promise.resolve();
  private inFlightCount = 0;

  constructor(opts: OrchestratorOptions) {
    if (!opts.verifierCmd || (typeof opts.verifierCmd === "string" && opts.verifierCmd.trim().length === 0) || (Array.isArray(opts.verifierCmd) && opts.verifierCmd.length === 0)) {
      throw new Error("MergeOrchestrator: verifierCmd wajib diisi (test suite repo)");
    }
    this.opts = {
      store: opts.store,
      verifierCmd: opts.verifierCmd,
      artifactDirBase: opts.artifactDirBase ?? "data/artifacts",
      worktreeBase: opts.worktreeBase ?? join(tmpdir(), "sk-ormerge"),
      approvalGranted: opts.approvalGranted ?? false,
      remoteUrl: opts.remoteUrl ?? "",
      remoteName: opts.remoteName ?? "origin",
      pushRetries: opts.pushRetries ?? 3,
      pushBackoffMs: opts.pushBackoffMs ?? [1000, 4000, 16000],
      committerName: opts.committerName ?? "Shorekeeper Orchestrator",
      committerEmail: opts.committerEmail ?? "orchestrator@shorekeeper.local",
      sleepMs: opts.sleepMs ?? delay,
      onEvent: opts.onEvent ?? (() => {}),
      onTaskClosed: opts.onTaskClosed ?? (() => {}),
      now: opts.now ?? (() => Date.now()),
    };
  }

  emit(evt: OrchestratorEvent): void {
    this.opts.onEvent(evt);
  }

  /**
   * Merge gate — antrean SEQUENTIAL (promise chain): dua task tidak pernah
   * di-merge paralel. `repoPath` = fixture repo tempat branch worker berada.
   * TASK-3.1: span `merge` + metric merge_duration_seconds (metadata only).
   */
  mergeTask(taskId: string, repoPath: string): Promise<MergeTaskResult> {
    const t0 = Date.now();
    const run = this.chain.then(() => {
      this.inFlightCount += 1;
      getObservability()?.tracer.childStart(taskId, "merge", { in_flight: this.inFlightCount });
      return this.mergeTaskInner(taskId, repoPath).finally(() => {
        this.inFlightCount -= 1;
      });
    });
    const instrumented = run.then(
      (res) => {
        this.endMergeSpan(taskId, res, t0, undefined);
        return res;
      },
      (err) => {
        this.endMergeSpan(taskId, null, t0, err);
        throw err;
      },
    );
    // chain menelan error agar satu task gagal tidak membatalkan antrean
    this.chain = instrumented.then(
      () => undefined,
      () => undefined,
    );
    return instrumented;
  }

  /** Tutup span merge + metric durasi (fail-open: tidak pernah throw). */
  private endMergeSpan(taskId: string, res: MergeTaskResult | null, t0: number, err: unknown): void {
    try {
      const otel = getObservability();
      if (!otel) return;
      const durSec = (Date.now() - t0) / 1000;
      const lane = this.opts.store.getTask(taskId)?.lane;
      const ok = res !== null && (res.status === "merged" || res.status === "empty");
      otel.tracer.childEnd(
        taskId,
        "merge",
        { merge_status: res?.status ?? "error", merge_commit: res?.mergeCommit ?? null },
        err !== undefined ? err : ok ? undefined : { code: res?.reason?.split(" ")[0] ?? res?.status ?? "MERGE_FAILED" },
      );
      otel.metrics.mergeDurationSeconds(durSec, taskId, lane);
    } catch {
      // observasi tidak boleh menghentikan orkestrasi
    }
  }

  /** Banyaknya merge yang sedang berjalan (untuk assert "tidak pernah > 1"). */
  inFlight(): number {
    return this.inFlightCount;
  }

  // ---------------------------------------------------------------------------

  private async mergeTaskInner(taskId: string, repoPath: string): Promise<MergeTaskResult> {
    const store = this.opts.store;
    const task = store.getTask(taskId);
    if (!task) return { status: "not_found", taskId };
    if (task.status !== "running" && task.status !== "blocked") {
      return { status: "bad_state", taskId, reason: `status=${task.status}` };
    }

    const branch = `${WORKER_BRANCH_PREFIX}${taskId}`;
    const artifactDir = resolve(join(this.opts.artifactDirBase, taskId));
    const branchSha = revParse(repoPath, branch);
    const branchEmpty = branchSha === null;

    // --- pre-merge conflict check (TASK-2.3, defense-in-depth) ---
    const overlap = branchSha ? mergeTreeOverlap(repoPath, "main", branch) : [];
    if (overlap.length > 0) {
      this.emit({
        type: "overlap",
        taskId,
        detail: `merge-tree overlap vs main: ${overlap.join(", ")} — sequential + file-by-file`,
      });
    }

    // --- main harus bersih sebelum squash ---
    const dirty = runGitSafe(repoPath, ["status", "--porcelain"]).stdout.trim();
    if (dirty.length > 0) {
      const rec = store.transition(taskId, "blocked", { error: `BLOCKED_GATE MainRepoDirty: ${dirty.slice(0, 200)}` });
      void rec;
      return { status: "blocked_gate", taskId, reason: dirty.slice(0, 200) };
    }

    let mergeCommit: string | null = null;
    if (branchSha) {
      // --- verifier read-only pada branch worker (sebelum menyentuh main) ---
      const ok = await this.verifyBranch(repoPath, branch);
      if (!ok) return this.reject(taskId, "VERIFY_FAILED");

      const base = runGitSafe(repoPath, ["merge-base", "main", branch]).stdout.trim();
      const ahead = base.length > 0 ? runGitSafe(repoPath, ["rev-list", "--count", `${base}..${branch}`]).stdout.trim() : "0";
      if (ahead !== "0" && ahead !== "") {
        // squash merge — satu commit di main; JANGAN --no-verify bypass
        const squash = runGitSafe(repoPath, ["merge", "--squash", "--no-edit", branch]);
        if (squash.exitCode !== 0) {
          runGitSafe(repoPath, ["merge", "--abort"]);
          if (overlap.length > 0) {
            // residual overlap: merge file-per-file oleh ORCHESTRATOR (bukan worker)
            try {
              for (const f of overlap) runGit(repoPath, ["checkout", branch, "--", f]);
              runGit(repoPath, ["add", "-A"]);
            } catch {
              return {
                status: "merge_conflict",
                taskId,
                reason: `squash conflict & file-by-file gagal: ${overlap.join(", ")}`,
              };
            }
            runGit(repoPath, [
              "-c",
              `user.name=${this.opts.committerName}`,
              "-c",
              `user.email=${this.opts.committerEmail}`,
              "commit",
              "-qm",
              `orchestrator(merge-file-by-file): ${taskId}`,
            ]);
          } else {
            return {
              status: "merge_conflict",
              taskId,
              reason: `squash merge conflict tanpa overlap terdeteksi — ${squash.stdout.slice(0, 300)}`,
            };
          }
        } else {
          runGit(repoPath, [
            "-c",
            `user.name=${this.opts.committerName}`,
            "-c",
            `user.email=${this.opts.committerEmail}`,
            "commit",
            "-qm",
            `orchestrator(merge): ${taskId}`,
          ]);
        }
        mergeCommit = revParse(repoPath, "main");
        // verifier ulang pada main (hasil akhir) — tetap wajib hijau
        if (mergeCommit) {
          const okFinal = await this.runVerifierAt(repoPath);
          if (!okFinal) {
            return this.reject(taskId, "VERIFY_FAILED", "post-merge verifier merah");
          }
        }
        this.emit({ type: "merged", taskId, detail: mergeCommit ?? "" });
        runGitSafe(repoPath, ["branch", "-D", branch]); // cleanup branch worker
      } else {
        // branch identik dengan main — tidak ada perubahan baru
        mergeCommit = revParse(repoPath, "main");
      }
    } else {
      // tidak ada branch worker: tanpa perubahan (atau diff kosong) → verifier main
      this.emit({ type: "verify", taskId, ok: true, detail: "no worker branch — verify main" });
      const ok = await this.runVerifierAt(repoPath);
      if (!ok) return this.reject(taskId, "VERIFY_FAILED");
    }

    const sha = mergeCommit ?? revParse(repoPath, "main");

    // --- approval gate push remote (default: main-local saja) ---
    this.updateMainLocal(repoPath, sha ?? "main");
    if (this.opts.approvalGranted && sha) {
      const push = await this.pushWithRetry(repoPath);
      if (!push.ok) {
        const msg =
          `PUSH_REJECTED setelah ${push.attempts} percobaan: ${push.message}. ` +
          `Manual: git -C ${repoPath} push ${this.opts.remoteName} main && tandai task done manual.`;
        store.transition(taskId, "failed", { error: msg });
        this.opts.onTaskClosed(taskId, "failed"); // release ownership → pump antrean ter-defer
        this.emit({ type: "push", taskId, ok: false, detail: msg });
        return { status: "push_rejected", taskId, mergeCommit: sha, reason: msg };
      }
      this.emit({ type: "push", taskId, ok: true, detail: `${push.attempts} attempts` });
    } else {
      this.emit({ type: "push", taskId, ok: true, detail: "no approval — main-local only (no remote push)" });
    }

    // --- done + merge_commit tercatat (artifact merge.json + summary store) ---
    const short = sha ? sha.slice(0, 7) : null;
    const mergeJson = {
      task_id: taskId,
      merge_commit: sha,
      merge_commit_short: short,
      merged_at: this.opts.now(),
      empty_merge: branchEmpty,
    };
    writeArtifact(artifactDir, "merge.json", mergeJson);
    if (task.artifact_dir === null) {
      // kontrak: DB hanya path — link via storeArtifactContent (isi SAMA, tidak menimpa info)
      try {
        store.storeArtifactContent(taskId, resolve(this.opts.artifactDirBase), "merge.json", JSON.stringify(mergeJson, null, 2));
      } catch {
        // best-effort: artifact_dir sudah di-set manager
      }
    }
    const baseSummary =
      task.summary && task.summary.trim().length > 0
        ? task.summary
        : `Worker selesai & diverifikasi (${taskId}); merge gate hijau.`;
    const summary =
      short && !baseSummary.includes(short)
        ? `${baseSummary.trim().replace(/\.$/, "")}. Squash merge: ${short}.`
        : baseSummary;
    if (summary.length === 0 || countWordsLocal(summary) > 200) {
      return this.reject(taskId, "SUMMARY_TOO_LONG");
    }
    // release ownership SEBELUM transisi done: task ter-defer (TASK-2.3) baru
    // boleh jalan setelah perubahan owner benar-benar ter-merge ke main.
    this.opts.onTaskClosed(taskId, "done");
    store.transition(taskId, "done", { summary, artifact_dir: artifactDir });
    this.emit({ type: "done", taskId, detail: `merge_commit=${short ?? "(none)"}` });
    return short
      ? { status: branchEmpty ? "empty" : "merged", taskId, mergeCommit: sha ?? undefined }
      : { status: "empty", taskId, reason: "tanpa commit (tidak ada perubahan)" };
  }

  // ---------------------------------------------------------------------------

  private reject(taskId: string, error: string, extra?: string): MergeTaskResult {
    this.opts.store.transition(taskId, "blocked", { error });
    const reason = `${error}${extra ? ` — ${extra}` : ""}`;
    this.emit({ type: "merge-rejected", taskId, detail: reason });
    return { status: "rejected", taskId, reason };
  }

  /** Verifier read-only pada branch worker via worktree sementara (tidak checkout main). */
  private async verifyBranch(repoPath: string, branch: string): Promise<boolean> {
    const wt = join(this.opts.worktreeBase, `vt-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`);
    try {
      runGit(repoPath, ["worktree", "add", "--detach", wt, branch]);
      return await this.runVerifierAt(wt);
    } finally {
      // pola removeWorktree bridge: remove → fallback rm -rf + prune (idempotent;
      // verifier worktree sementara tidak boleh bocor ke end-state E2E).
      try {
        runGit(repoPath, ["worktree", "remove", "--force", wt]);
      } catch {
        try {
          rmSync(wt, { recursive: true, force: true });
        } catch {
          // best-effort
        }
        runGitSafe(repoPath, ["worktree", "prune"]);
      }
    }
  }

  /** Verifier read-only pada cwd repo (main). */
  private async runVerifierAt(cwd: string): Promise<boolean> {
    let out = "";
    let ok = false;
    try {
      const { file, args } = parseVerifierCommand(this.opts.verifierCmd);
      out = execFileSync(file, args, {
        cwd,
        encoding: "utf8",
        env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTEST_DISABLE_PLUGIN_AUTOLOAD: "1" },
      });
      ok = true;
    } catch (err) {
      out = `${(err as { stdout?: unknown }).stdout ?? ""}${(err as { stderr?: unknown }).stderr ?? ""}`;
    }
    const tail = out.split("\n").slice(-8).join("\n").trim();
    this.emit({ type: "verify", taskId: "?", ok, detail: tail.slice(0, 300) });
    return ok;
  }

  private updateMainLocal(repoPath: string, ref: string): void {
    try {
      runGit(repoPath, ["branch", "-f", "main-local", ref]);
      this.emit({ type: "main-local", taskId: ref, ok: true });
    } catch {
      this.emit({ type: "main-local", taskId: ref, ok: false });
    }
  }

  /** Retry 3× backoff (1s/4s/16s default) saat push ditolak remote. */
  private async pushWithRetry(repoPath: string): Promise<{ ok: boolean; attempts: number; message: string }> {
    const remoteName = this.opts.remoteName;
    if (this.opts.remoteUrl) {
      runGitSafe(repoPath, ["remote", "remove", remoteName]);
      runGitSafe(repoPath, ["remote", "add", remoteName, this.opts.remoteUrl]);
    }
    const remotes = runGitSafe(repoPath, ["remote"]).stdout.trim().split("\n").filter(Boolean);
    if (!remotes.includes(remoteName)) {
      return { ok: false, attempts: 0, message: `remote "${remoteName}" tidak terkonfigurasi` };
    }
    let lastErr = "";
    const maxAttempts = Math.max(1, this.opts.pushRetries);
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      const r = runGitSafe(repoPath, ["push", remoteName, "main:main"]);
      if (r.exitCode === 0) return { ok: true, attempts: attempt, message: "" };
      lastErr = r.stdout.trim().slice(0, 300) || `push gagal (attempt ${attempt})`;
      if (attempt < maxAttempts) await this.opts.sleepMs(this.opts.pushBackoffMs[attempt - 1] ?? 1000);
    }
    return { ok: false, attempts: maxAttempts, message: lastErr };
  }
}

/** Event hook (dipakai E2E/logging). */
export function countWordsLocal(s: string): number {
  return s.trim().length === 0 ? 0 : s.trim().split(/\s+/).length;
}

// ---------------------------------------------------------------------------
// util artifact fs (kecil, tanpa dep)
// ---------------------------------------------------------------------------

function writeArtifact(dir: string, name: string, content: unknown): void {
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, name), typeof content === "string" ? content : JSON.stringify(content, null, 2));
}