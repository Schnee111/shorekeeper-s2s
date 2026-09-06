/**
 * contracts.ts — Shorekeeper handoff contract & task record schema (zod).
 *
 * Source of truth: docs/api.md §2 (handoff-contract). Breaking field change
 * => bump CONTRACT_VERSION; jangan ubah in-place.
 */
import { z } from "zod";

export const CONTRACT_VERSION = "1";

// ---------------------------------------------------------------------------
// 2.1 Handoff contract (front -> orchestrator)
// ---------------------------------------------------------------------------

export const EntitySchema = z.object({
  type: z.string().min(1, "entity.type wajib diisi"),
  value: z.string().min(1, "entity.value wajib diisi"),
});

export const HandoffSchema = z
  .object({
    /** Intent user dalam 1 kalimat, contoh: "kerjakan issue #12 di repo X" */
    intent: z.string().min(1, "intent wajib diisi (tidak boleh kosong)"),
    /** Entitas yang diekstrak front (repo, issue, file, dsb) */
    entities: z.array(EntitySchema).default([]),
    /** Referensi transcript (room + timestamp) untuk audit */
    transcript_ref: z.string().min(1, "transcript_ref wajib diisi"),
    /** Keyakinan routing 0..1 */
    confidence: z.number().min(0).max(1, "confidence harus number 0..1"),
    /** Bahasa percakapan (default id) */
    language: z.string().min(2).max(8).default("id"),
  })
  .strict();

export type Handoff = z.infer<typeof HandoffSchema>;

// ---------------------------------------------------------------------------
// 2.2 Task record (task store)
// ---------------------------------------------------------------------------

export const TaskStatus = z.enum([
  "queued",
  "running",
  "done",
  "failed",
  "cancelled",
  "blocked",
  "waiting_input",
  "unknown",
]);
export type TaskStatus = z.infer<typeof TaskStatus>;

export const LANES = ["research", "frontend", "debug", "qa"] as const;
export const LaneSchema = z.enum(LANES);

/** State machine: transitions per ADR and Gap Analysis (P1) */
export const TASK_TRANSITIONS: Record<TaskStatus, readonly TaskStatus[]> = {
  queued: ["running", "cancelled"],
  running: ["done", "failed", "cancelled", "blocked", "waiting_input", "unknown"],
  blocked: ["running", "cancelled", "failed"],
  waiting_input: ["running", "cancelled", "failed"],
  unknown: ["running", "done", "failed", "cancelled"],
  done: [],
  failed: [],
  cancelled: [],
};

export function canTransition(from: TaskStatus, to: TaskStatus): boolean {
  return TASK_TRANSITIONS[from]?.includes(to) ?? false;
}

export const summaryMaxWords = 200;

export const TASK_ID_REGEX = /^[a-zA-Z0-9_-]{1,64}$/;
export const TaskIdSchema = z
  .string()
  .min(1)
  .max(64)
  .regex(TASK_ID_REGEX, "task_id hanya boleh alfanumerik, underscore, dan tanda hubung (1-64 karakter)");

export const TaskRecordSchema = z.object({
  task_id: TaskIdSchema,
  session_room: z.string().default(""),
  user_intent: z.string().default(""),
  parent_id: z.string().nullable().default(null),
  root_task_id: z.string().nullable().default(null),
  lane: LaneSchema.default("debug"),
  status: TaskStatus.default("queued"),
  worker_pid: z.number().int().nullable().default(null),
  heartbeat_ts: z.number().int().nullable().default(null),
  created_at: z.number().int(),
  started_at: z.number().int().nullable().default(null),
  finished_at: z.number().int().nullable().default(null),
  contract_ref: z.string().default(""),
  artifact_dir: z.string().nullable().default(null),
  /** Hasil akhir untuk voice — WAJIB <= 200 kata (kontrak voice) */
  summary: z
    .string()
    .default("")
    .refine((s) => s.trim().length === 0 || s.trim().split(/\s+/).length <= summaryMaxWords, {
      message: `summary melebihi ${summaryMaxWords} kata (kontrak voice)`,
    }),
  error: z.string().nullable().default(null),
  notify_gate: z.enum(["idle", "next_turn", "off"]).default("next_turn"),
  priority: z.number().int().default(1),
});

export type TaskRecord = z.infer<typeof TaskRecordSchema>;

// ---------------------------------------------------------------------------
// 2.3 Task spec (orchestrator -> worker) — contract-first decomposition
// ---------------------------------------------------------------------------

export const TaskSpecSchema = z
  .object({
    task_id: TaskIdSchema,
    lane: LaneSchema.default("debug"),
    /** Objective 1 kalimat */
    objective: z.string().min(1, "objective wajib diisi"),
    /** One-file-one-owner: file yang BOLEH diubah worker */
    files_owned: z.array(z.string()).default([]),
    /** Requirements bernomor: input/output/error */
    requirements: z.array(z.string()).default([]),
    /** Acceptance criteria yang testable */
    acceptance_criteria: z.array(z.string()).min(1, "acceptance_criteria minimal 1"),
    /** Batas repo/lingkungan (path terlarang dst) */
    boundaries: z.array(z.string()).default([]),
    /** Langkah verifikasi eksplisit (test runner + command) */
    verification_steps: z.array(z.string()).default([]),
  })
  .strict();

export type TaskSpec = z.infer<typeof TaskSpecSchema>;