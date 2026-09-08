import { useState } from "react";
import { api } from "../api";
import { buttonCheckGoing } from "../checkup";
import { useStore } from "../state";
import { targetAddress } from "../components/CampaignModal";
import {
  ActionButton, BulkBar, Confirm, DataTable, Field, FormError, FormFooter, Modal,
  PageHead, Status, Switch, TextArea, byId, fmtTime, useBulk, useHashFocus, useSelection,
  useSubmit,
} from "../components/ui";
import type { Column } from "../components/ui";
import type { Target } from "../types";

export function Targets() {
  const { snapshot, t, act, lang } = useStore();
  const [adding, setAdding] = useState(false);
  // Windows keep ids and read the live records: see byId.
  const [removing, setRemoving] = useState<string | null>(null);
  const [bulkDelete, setBulkDelete] = useState(false);
  const selection = useSelection((snapshot?.targets ?? []).map((x) => x.id));
  const bulk = useBulk("targets", selection);
  useHashFocus();

  if (!snapshot) return null;
  const checkup = snapshot.checkup;
  const mine = checkup.running && checkup.kind === "targets";
  const checking = buttonCheckGoing(checkup);
  const removed = byId(snapshot.targets, removing);

  /** A short word, never Telegram's sentence: the full text of a failed
   *  check is an issue, and an issue lives in the dot's tooltip. */
  const availability = (target: Target) => {
    if (!target.active) return t("state.DISABLED");
    if (!target.last_check) return t("targets.unchecked");
    const code = target.last_check_error?.code;
    if (code === "delivery.not_found") return t("targets.not_found");
    if (code === "delivery.join_to_comment") return t("targets.needs_join");
    return target.last_check_error ? t("targets.unavailable") : t("targets.available");
  };
  const title = (x: Target) => x.title || targetAddress(x);

  const columns: Column<Target>[] = [
    { key: "title", label: t("common.name"), value: title,
      render: (x) => <span title={title(x)}>{title(x)}</span> },
    { key: "address", label: t("targets.username"), width: 150, value: targetAddress,
      render: (x) => <span className="mono muted" title={targetAddress(x)}>{targetAddress(x)}</span> },
    { key: "status", label: t("common.status"), width: 140, value: availability,
      render: (x) => (
        <Status state={x.effective} issues={x.issues} label={availability(x)}
                tone={x.active && !x.last_check ? "idle" : undefined} />
      ) },
    { key: "type", label: t("targets.type"), width: 110,
      value: (x) => t.dyn(`targets.type.${x.type}`),
      render: (x) => <span className="muted">{t.dyn(`targets.type.${x.type}`)}</span> },
    { key: "checked", label: t("accounts.last_check"), width: 100,
      value: (x) => x.last_check ?? "",
      render: (x) => <span className="muted">{fmtTime(x.last_check, lang, t("common.never"))}</span> },
    { key: "active", label: t("targets.active"), width: 56,
      render: (x) => (
        <Switch checked={x.active}
                onChange={(v) => act(() => api.targets.update({ id: x.id, active: v }))} />
      ) },
    { key: "actions", label: "", width: "var(--w-actions-2)", className: "actions",
      render: (x) => (
        <span className="ab-row">
          <ActionButton size="sm" variant="ghost" disabled={checking}
                        onClick={() => act(() => api.targets.check(x.id))}>
            {t("common.check")}
          </ActionButton>
          <ActionButton size="sm" variant="ghost-danger" onClick={() => setRemoving(x.id)}>
            {t("common.delete")}
          </ActionButton>
        </span>
      ) },
  ];

  return (
    <>
      <PageHead
        title={t("targets.title")}
        sub={t("targets.sub")}
        actions={
          <>
            {/* The same background run the accounts page starts, with the
                same progress on the button. */}
            <ActionButton disabled={checking} onClick={() => act(() => api.targets.check())}>
              {mine ? t("targets.checking_progress", { done: checkup.done, total: checkup.total })
                    : t("targets.check_all")}
            </ActionButton>
            <ActionButton variant="primary" onClick={() => setAdding(true)}>
              {t("targets.add")}
            </ActionButton>
          </>
        }
      />

      <BulkBar selection={selection}>
        <ActionButton size="sm" disabled={checking}
                      onClick={() => act(() => api.bulk.check("targets", selection.ids))}>
          {t("common.check")}
        </ActionButton>
        <ActionButton size="sm" onClick={() => bulk.run("enable")}>{t("common.enable")}</ActionButton>
        <ActionButton size="sm" onClick={() => bulk.run("disable")}>{t("common.disable")}</ActionButton>
        <ActionButton size="sm" variant="danger" onClick={() => setBulkDelete(true)}>
          {t("common.delete")}
        </ActionButton>
      </BulkBar>

      <div className="card">
        <DataTable columns={columns} rows={snapshot.targets} rowKey={(x) => x.id}
                   rowId={(x) => x.id} selection={selection} empty={t("targets.empty")} />
      </div>

      {bulkDelete && (
        <Confirm text={t("bulk.confirm_delete", { n: selection.count })}
                 onCancel={() => setBulkDelete(false)}
                 onConfirm={() => { setBulkDelete(false); void bulk.run("delete"); }} />
      )}
      {adding && <AddModal onClose={() => setAdding(false)} />}
      {removed && (
        <Confirm text={t("common.confirm_delete", { name: title(removed) })}
                 onCancel={() => setRemoving(null)}
                 onConfirm={() => {
                   const id = removed.id;
                   setRemoving(null);
                   void act(() => api.targets.remove(id));
                 }} />
      )}
    </>
  );
}

/** Add one channel or a pasted list. A refusal keeps the window and what
 *  was typed in it. */
function AddModal({ onClose }: { onClose: () => void }) {
  const { t, notify } = useStore();
  const [text, setText] = useState("");
  const submit = useSubmit();

  async function save() {
    const ok = await submit.run(async () => {
      const result = await api.targets.add(text);
      // a multi-line paste comes back as a summary rather than a single target
      if (result && typeof result === "object" && "added" in result) {
        const { added, skipped } = result as { added: number; skipped: number };
        notify(t("targets.added", { added, skipped }), "ok");
      }
    });
    if (ok) onClose();
  }

  return (
    <Modal title={t("targets.add")} onClose={onClose}
           footer={<FormFooter onCancel={onClose} onConfirm={save} busy={submit.busy}
                               disabled={!text.trim()} confirm={t("common.add")} />}>
      <FormError message={submit.error} />
      <Field label={t("targets.link")} hint={t("targets.add_hint")}>
        <TextArea autoFocus value={text}
                  placeholder={"@channel\nhttps://t.me/another\n-1001234567890"}
                  onChange={setText} />
      </Field>
    </Modal>
  );
}
