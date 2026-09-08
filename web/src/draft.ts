/** The draft of a form over a record the server keeps changing.
 *
 *  A window shows the saved record with the user's changes on top. An edit
 *  is kept together with the saved value it was made against: a field
 *  nobody touched shows the server's value as it moves, a touched field
 *  keeps what the user chose - until the server changes that same field,
 *  and then the server wins. The app switching an account off while its
 *  window is open must not be undone by that window's Save.
 *
 *  Values are compared with Object.is, so a form keeps plain values -
 *  strings, numbers, booleans - in its fields. No React here: this is the
 *  part that is tested on its own (web/tests).
 */

export type Fields = Record<string, string | number | boolean>;

export type Edits<V extends Fields> = { [K in keyof V]?: { value: V[K]; base: V[K] } };

/** The edits that still stand against this state of the server. */
export function standing<V extends Fields>(server: V, edits: Edits<V>): Edits<V> {
  const out: Edits<V> = {};
  for (const key of Object.keys(edits) as (keyof V)[]) {
    const edit = edits[key]!;
    if (Object.is(edit.base, server[key]) && !Object.is(edit.value, server[key])) {
      out[key] = edit;
    }
  }
  return out;
}

/** What the form shows. */
export function shown<V extends Fields>(server: V, edits: Edits<V>): V {
  const out = { ...server };
  for (const key of Object.keys(edits) as (keyof V)[]) out[key] = edits[key]!.value;
  return out;
}

/** The edits once the user sets one field. Setting it back to the saved
 *  value is no edit at all. */
export function edited<V extends Fields, K extends keyof V>(
  server: V, edits: Edits<V>, key: K, value: V[K]): Edits<V> {
  const out = { ...edits };
  if (Object.is(value, server[key])) delete out[key];
  else out[key] = { value, base: server[key] };
  return out;
}

/** The fields that differ from the server: what a save sends. */
export function changes<V extends Fields>(edits: Edits<V>): Partial<V> {
  const out: Partial<V> = {};
  for (const key of Object.keys(edits) as (keyof V)[]) out[key] = edits[key]!.value;
  return out;
}
