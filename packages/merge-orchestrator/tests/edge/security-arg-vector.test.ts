import { describe, it, expect } from "vitest";
import { parseVerifierCommand } from "../../src/orchestrator.js";

describe("Security regression test: Verifier argument vector (#26)", () => {
  it("parses single string into direct binary and args array without shell invocation", () => {
    const res = parseVerifierCommand("pytest -q tests/unit --maxfail=1");
    expect(res.file).toBe("pytest");
    expect(res.args).toEqual(["-q", "tests/unit", "--maxfail=1"]);
  });

  it("safely handles quoted arguments without expanding shell metacharacters", () => {
    const res = parseVerifierCommand('node -e "console.log(process.env.PATH)"');
    expect(res.file).toBe("node");
    expect(res.args).toEqual(["-e", "console.log(process.env.PATH)"]);
  });

  it("handles array commands directly", () => {
    const res = parseVerifierCommand(["python3", "-m", "unittest", "discover"]);
    expect(res.file).toBe("python3");
    expect(res.args).toEqual(["-m", "unittest", "discover"]);
  });
});
