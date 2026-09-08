/** The one rule of every «Проверить» button.
 *
 *  One check runs at a time. The background run never stands in a
 *  button's way - the button stops it and starts its own, and what the
 *  background had not reached goes back as it was - so only a run another
 *  button started makes the buttons wait. The same test tells a window
 *  that the check it started is over. No React here: tested on its own
 *  (web/tests).
 */
import type { Checkup } from "./types";

/** A check a button started is going. */
export function buttonCheckGoing(checkup: Checkup): boolean {
  return checkup.running && checkup.kind !== "background";
}
