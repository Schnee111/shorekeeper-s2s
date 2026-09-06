import { describe, it, expect } from "vitest";
import { parseVerifierCommand, MergeOrchestrator } from "../../src/orchestrator.js";
import { TaskStore } from "task-store";
import { join } from "node:path";
import { tmpdir } from "node:os";

describe("MergeOrchestrator safe verifier execution (BUG-3)", () => {
  it("rejects verifierCmd containing unsafe shell chaining characters or parses them safely without subshell interpolation", () => {
    const pwnFile = join(tmpdir(), `sk-pwned-${Date.now()}`);
    expect(parseVerifierCommand(`echo ok; touch ${pwnFile}`)).toEqual({
      file: "echo",
      args: ["ok;", "touch", pwnFile],
    }); // Does not interpret ';' as shell command separator
  });

  it("supports array-style command vector [file, ...args]", () => {
    const store = new TaskStore({ dbPath: ":memory:" });
    const orc = new MergeOrchestrator({
      store,
      verifierCmd: ["node", "-e", "process.exit(0)"],
    });
    expect(orc).toBeDefined();
    expect(parseVerifierCommand(["python", "-m", "pytest", "-q"])).toEqual({
      file: "python",
      args: ["-m", "pytest", "-q"],
    });
  });
});
