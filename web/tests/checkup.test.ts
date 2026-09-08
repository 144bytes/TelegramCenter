/** The one rule of every «Проверить» button (src/checkup.ts). */
import test from "node:test";
import assert from "node:assert/strict";
import { buttonCheckGoing } from "../src/checkup.js";
import type { Checkup } from "../src/types.js";

const idle: Checkup = { running: false, total: 0, done: 0, current: null, spam: false, kind: "" };

test("nothing running: the buttons can start a check", () => {
  assert.equal(buttonCheckGoing(idle), false);
});

test("the background round never makes the buttons wait", () => {
  assert.equal(buttonCheckGoing({ ...idle, running: true, kind: "background" }), false);
});

test("a run another button started does", () => {
  for (const kind of ["accounts", "operators", "targets", "spam"] as const) {
    assert.equal(buttonCheckGoing({ ...idle, running: true, kind }), true, kind);
  }
});

test("a finished run is over whatever its kind", () => {
  assert.equal(buttonCheckGoing({ ...idle, kind: "accounts", done: 1, total: 1 }), false);
});
