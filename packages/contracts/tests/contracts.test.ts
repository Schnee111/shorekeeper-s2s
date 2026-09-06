import { describe, expect, it } from "vitest";
import {
  CONTRACT_VERSION,
  TaskRecordSchema,
  TaskSpecSchema,
  canTransition,
  HandoffSchema,
} from "../src/index.js";

const validHandoff = {
  intent: "kerjakan issue #12 di repo X",
  entities: [
    { type: "repo", value: "repo-a" },
    { type: "issue", value: "#12" },
  ],
  transcript_ref: "room_abc/2026-08-17T04:00:00Z",
  confidence: 0.92,
  language: "id",
};

describe("HandoffSchema (handoff contract)", () => {
  it("fixture valid dari contoh 'kerjakan issue #12 di repo X' parse OK", () => {
    const parsed = HandoffSchema.parse(validHandoff);
    expect(parsed.intent).toBe("kerjakan issue #12 di repo X");
    expect(parsed.entities).toHaveLength(2);
    expect(parsed.confidence).toBe(0.92);
  });

  it("missing intent => reject dengan pesan field 'intent'", () => {
    const { intent: _intent, ...broken } = validHandoff;
    const result = HandoffSchema.safeParse(broken);
    expect(result.success).toBe(false);
    if (!result.success) {
      const msg = JSON.stringify(result.error.issues[0]?.path);
      expect(msg).toContain("intent");
    }
  });

  it("confidence bukan number => reject dengan pesan field 'confidence'", () => {
    const broken = { ...validHandoff, confidence: "high" };
    const result = HandoffSchema.safeParse(broken);
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(JSON.stringify(result.error.issues[0]?.path)).toContain("confidence");
    }
  });

  it("confidence di luar 0..1 => reject", () => {
    const result = HandoffSchema.safeParse({ ...validHandoff, confidence: 1.5 });
    expect(result.success).toBe(false);
  });
});

describe("TaskRecordSchema (task record)", () => {
  it("record lengkap valid", () => {
    const rec = TaskRecordSchema.parse({
      task_id: "task_de_01",
      session_room: "shore-room",
      user_intent: "perbaiki bug login",
      lane: "debug",
      status: "queued",
      created_at: Date.now(),
      priority: 1,
    });
    expect(rec.status).toBe("queued");
    expect(rec.summary).toBe("");
  });

  it("status invalid => reject", () => {
    const result = TaskRecordSchema.safeParse({
      task_id: "t1",
      status: "in_progress",
      created_at: Date.now(),
    });
    expect(result.success).toBe(false);
  });

  it("summary > 200 kata => reject (kontrak voice)", () => {
    const rec = {
      task_id: "t1",
      status: "done",
      created_at: Date.now(),
      summary: Array.from({ length: 201 }, () => "kata").join(" "),
    };
    const result = TaskRecordSchema.safeParse(rec);
    expect(result.success).toBe(false);
  });
});

describe("state machine", () => {
  it("transisi legal", () => {
    expect(canTransition("queued", "running")).toBe(true);
    expect(canTransition("running", "done")).toBe(true);
    expect(canTransition("running", "blocked")).toBe(true);
    expect(canTransition("running", "waiting_input")).toBe(true);
    expect(canTransition("running", "unknown")).toBe(true);
    expect(canTransition("waiting_input", "running")).toBe(true);
    expect(canTransition("blocked", "running")).toBe(true);
    expect(canTransition("running", "failed")).toBe(true);
    expect(canTransition("unknown", "running")).toBe(true);
  });

  it("transisi illegal ditolak", () => {
    expect(canTransition("done", "running")).toBe(false);
    expect(canTransition("failed", "queued")).toBe(false);
    expect(canTransition("cancelled", "done")).toBe(false);
  });
});

describe("TaskSpecSchema", () => {
  it("spec valid", () => {
    const spec = TaskSpecSchema.parse({
      task_id: "task_de_01",
      lane: "debug",
      objective: "fix bug: fungsi add salah return",
      files_owned: ["lib/math.py", "tests/test_math.py"],
      acceptance_criteria: ["pytest hijau"],
      verification_steps: ["uv run pytest -q"],
    });
    expect(spec.objective).toContain("add");
  });

  it("tanpa acceptance_criteria => reject", () => {
    const result = TaskSpecSchema.safeParse({
      task_id: "t1",
      objective: "x",
    });
    expect(result.success).toBe(false);
  });
});

describe("versioning", () => {
  it("CONTRACT_VERSION = 1 (breaking change => bump, jangan ubah in-place)", () => {
    expect(CONTRACT_VERSION).toBe("1");
  });

  it("task_id dengan flag injection atau path traversal => reject", () => {
    const maliciousIds = [
      "--upload-pack=evil",
      "../traversal",
      "task/subtask",
      "task;rm -rf /",
      "task id with space",
      "task$evil",
    ];
    for (const tid of maliciousIds) {
      const res1 = TaskRecordSchema.safeParse({
        task_id: tid,
        created_at: Date.now(),
      });
      expect(res1.success, `TaskRecordSchema should reject ${tid}`).toBe(false);

      const res2 = TaskSpecSchema.safeParse({
        task_id: tid,
        objective: "clean code",
        acceptance_criteria: ["ok"],
      });
      expect(res2.success, `TaskSpecSchema should reject ${tid}`).toBe(false);
    }
  });

  it("task_id valid format alfanumerik, dash, underscore => pass", () => {
    const validIds = ["task-123", "TASK_456", "task-a_b-1", "012345"];
    for (const tid of validIds) {
      const res = TaskRecordSchema.safeParse({
        task_id: tid,
        created_at: Date.now(),
      });
      expect(res.success, `TaskRecordSchema should accept ${tid}`).toBe(true);
    }
  });
});