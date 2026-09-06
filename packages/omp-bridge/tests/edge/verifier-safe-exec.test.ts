import { describe, it, expect } from "vitest";
import { WorkerManager, parseVerifierCmd } from "../../src/manager.js";
import { TaskStore } from "task-store";

describe("WorkerManager safe verifier execution (BUG-3)", () => {
  it("parses verifier command into safe argument vectors without shell invocation", () => {
    const parsed = parseVerifierCmd("python -m pytest -q tests");
    expect(parsed).toEqual({
      file: "python",
      args: ["-m", "pytest", "-q", "tests"],
    });
  });

  it("supports array-style command vector in WorkerManager options", () => {
    const store = new TaskStore({ dbPath: ":memory:" });
    const mgr = new WorkerManager({
      store,
      allowlist: ["/tmp"],
      verifierCmd: ["pytest", "-q"],
    });
    expect(mgr).toBeDefined();
    expect(parseVerifierCmd(["node", "--check", "file.js"])).toEqual({
      file: "node",
      args: ["--check", "file.js"],
    });
  });
});
