import { z } from "zod";
import { LaneSchema, TaskIdSchema, TaskStatus } from "./contracts.js";

// ---------------------------------------------------------------------------
// Commands (Imperative actions requested by Voice or CLI)
// ---------------------------------------------------------------------------

export const CreateTaskCommandSchema = z.object({
  task_id: TaskIdSchema,
  session_room: z.string().default(""),
  user_intent: z.string().min(1),
  lane: LaneSchema.default("debug"),
  parent_id: z.string().nullable().optional(),
  root_task_id: z.string().nullable().optional(),
  priority: z.number().int().default(1),
});
export type CreateTaskCommand = z.infer<typeof CreateTaskCommandSchema>;

export const StopTaskCommandSchema = z.object({
  task_id: TaskIdSchema,
  reason: z.string().default("cancelled by user"),
});
export type StopTaskCommand = z.infer<typeof StopTaskCommandSchema>;

export const ResumeTaskCommandSchema = z.object({
  task_id: TaskIdSchema,
  user_response: z.string().min(1),
});
export type ResumeTaskCommand = z.infer<typeof ResumeTaskCommandSchema>;

// ---------------------------------------------------------------------------
// Receipts (Immediate structured acknowledgment for Front Voice Agent)
// ---------------------------------------------------------------------------

export const TaskReceiptSchema = z.object({
  accepted: z.boolean(),
  task_id: TaskIdSchema,
  status: TaskStatus,
  mode: z.enum(["inline", "background"]).default("background"),
  created_at: z.number().int(),
});
export type TaskReceipt = z.infer<typeof TaskReceiptSchema>;
