/** A window's draft over a record the server keeps changing (src/draft.ts). */
import test from "node:test";
import assert from "node:assert/strict";
import { changes, edited, shown, standing } from "../src/draft.js";
import type { Edits } from "../src/draft.js";

const saved = { proxy: "p1", disabled: true, notes: "" };

/** What the window shows and would save, the way useDraft reads it. */
function view(server: typeof saved, edits: Edits<typeof saved>) {
  const live = standing(server, edits);
  return { shown: shown(server, live), changes: changes(live) };
}

test("a field nobody touched follows the server", () => {
  const edits = edited(saved, {}, "notes", "VIP");
  const moved = { ...saved, proxy: "p2" };

  assert.equal(view(moved, edits).shown.proxy, "p2");
  assert.deepEqual(view(moved, edits).changes, { notes: "VIP" });
});

test("switched on in the window, the account can be checked at once", () => {
  // the window shows the draft, and the check button reads the same value
  const edits = edited(saved, {}, "disabled", false);

  assert.equal(view(saved, edits).shown.disabled, false);
  assert.deepEqual(view(saved, edits).changes, { disabled: false });
});

test("the server wins when it changes the field the user touched", () => {
  // picked p2 in the window; meanwhile the app moved the account to p3
  const edits = edited(saved, {}, "proxy", "p2");
  const moved = { ...saved, proxy: "p3" };

  assert.equal(view(moved, edits).shown.proxy, "p3");
  assert.deepEqual(view(moved, edits).changes, {});
});

test("the app switching the account off is not undone by the window", () => {
  const on = { ...saved, disabled: false };
  const edits = edited(on, {}, "notes", "VIP");
  const off = { ...on, disabled: true };

  assert.equal(view(off, edits).shown.disabled, true);
  assert.deepEqual(view(off, edits).changes, { notes: "VIP" }, "Save sends no switch");
});

test("setting a field back to the saved value is no change", () => {
  const there = edited(saved, {}, "proxy", "p2");
  const back = edited(saved, there, "proxy", "p1");

  assert.deepEqual(view(saved, back).changes, {});
});

test("a save the server took leaves nothing to save", () => {
  const edits = edited(saved, {}, "proxy", "p2");
  const stored = { ...saved, proxy: "p2" };

  assert.deepEqual(view(stored, edits).changes, {});
  assert.equal(view(stored, edits).shown.proxy, "p2");
});
