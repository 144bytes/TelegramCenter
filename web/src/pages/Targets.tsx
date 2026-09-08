import { useState } from "react";
import { api } from "../api";
import { useStore } from "../state";
import {
  Confirm, DataTable, Field, IssueDot, Modal, PageHead, Switch, fmtTime,
} from "../components/ui";
import type { Column } from "../components/ui";
import type { Target } from "../types";

export function Targets() {
  const { snapshot, t, act } = useStore();
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<Target | null>(null);

  if (!snapshot) return null;

  const availability = (target: Target) => {
    if (!target.last_check_status) return t("targets.unchecked");
    return target.last_check_status.startsWith("AVAILABLE")
      ? t("targets.available")
      : target.last_check_status;
  };

  const columns: Column<Target>[] = [
    {
      key: "dot", label: "", width: 26, sortable: false,
      render: (x) => <IssueDot issues={x.issues} state={x.effective} />,
    },
    {
      key: "title", label: t("common.name"),
      value: (x) => x.title || x.username,
      render: (x) => (
        <span className="truncate" style={{ display: "block", maxWidth: 260 }}
              title={x.title || `@${x.username}`}>
          {x.title || `@${x.username}`}
        </span>
      ),
    },
    {
      key: "link", label: t("targets.link"), width: 240,
      value: (x) => x.link,
      render: (x) => <span className="mono muted truncate">{x.link}</span>,
    },
    {
      key: "type", label: t("targets.type"), width: 110,
      value: (x) => x.type,
      render: (x) => <span className="muted">{x.type}</span>,
    },
    {
      key: "avail", label: t("targets.availability"), width: 190,
      value: (x) => availability(x),
      render: (x) => (
        <span className="muted truncate" title={x.last_check_status ?? ""}>
          {availability(x)}
        </span>
      ),
    },
    {
      key: "checked", label: t("accounts.last_check"), width: 120,
      value: (x) => x.last_check ?? "",
      render: (x) => (
        <span className="faint">{fmtTime(x.last_check, t("common.never"))}</span>
      ),
    },
    {
      key: "active", label: t("common.enabled"), width: 80, sortable: false,
      render: (x) => (
        <Switch checked={x.active} onChange={(v) =>
          act(() => api.targets.update({ id: x.id, active: v }))} />
      ),
    },
    {
      key: "actions", label: "", sortable: false, className: "actions",
      render: (x) => (
        <>
          <button className="btn sm ghost"
                  onClick={() => act(() => api.targets.check(x.id))}>
            {t("common.check")}
          </button>
          <button className="btn sm danger" onClick={() => setRemoving(x)}>
            {t("common.delete")}
          </button>
        </>
      ),
    },
  ];

  return (
    <>
      <PageHead
        title={t("targets.title")}
        sub={t("targets.sub")}
        actions={
          <>
            <button className="btn" onClick={() => act(() => api.targets.check())}>
              {t("targets.check_all")}
            </button>
            <button className="btn accent" onClick={() => setAdding(true)}>
              {t("targets.add")}
            </button>
          </>
        }
      />

      <div className="card">
        <DataTable columns={columns} rows={snapshot.targets} rowKey={(x) => x.id}
                   empty={t("targets.empty")} />
      </div>

      {adding && <AddModal onClose={() => setAdding(false)} />}
      {removing && (
        <Confirm
          text={t("common.confirm_delete", {
            name: removing.title || removing.username,
          })}
          onCancel={() => setRemoving(null)}
          onConfirm={() => {
            const id = removing.id;
            setRemoving(null);
            void act(() => api.targets.remove(id));
          }}
        />
      )}
    </>
  );
}

function AddModal({ onClose }: { onClose: () => void }) {
  const { t, act, notify } = useStore();
  const [text, setText] = useState("");

  async function save() {
    const result = await act(() => api.targets.add(text));
    // a multi-line paste comes back as a summary rather than a single target
    if (result && typeof result === "object" && "added" in result) {
      const { added, skipped } = result as { added: number; skipped: number };
      notify(t("targets.added", { added, skipped }), "ok");
    }
    onClose();
  }

  return (
    <Modal
      title={t("targets.add")}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>{t("common.cancel")}</button>
          <button className="btn accent" onClick={save} disabled={!text.trim()}>
            {t("common.add")}
          </button>
        </>
      }
    >
      <Field label={t("targets.link")} hint={t("targets.add_hint")}>
        <textarea className="textarea" autoFocus value={text}
                  placeholder={"@channel\nhttps://t.me/another\n-1001234567890"}
                  onChange={(e) => setText(e.target.value)} />
      </Field>
    </Modal>
  );
}
