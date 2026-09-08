import { useState } from "react";
import { api } from "../api";
import { useStore } from "../state";
import { Confirm, DataTable, Field, Modal, PageHead, fmtTime } from "../components/ui";
import type { Column } from "../components/ui";
import type { Template } from "../types";

export function Templates() {
  const { snapshot, t, act } = useStore();
  const [editing, setEditing] = useState<Template | null>(null);
  const [creating, setCreating] = useState(false);
  const [removing, setRemoving] = useState<Template | null>(null);

  if (!snapshot) return null;

  const usedBy = (id: string) =>
    snapshot.campaigns.filter((c) => c.source_template_id === id).length;

  const columns: Column<Template>[] = [
    {
      key: "name", label: t("common.name"), width: 220,
      value: (x) => x.name,
      render: (x) => x.name,
    },
    {
      key: "text", label: t("templates.text"),
      value: (x) => x.text,
      render: (x) => (
        <span className="muted truncate" style={{ display: "block", maxWidth: 460 }}>
          {x.text.replace(/\s+/g, " ")}
        </span>
      ),
    },
    {
      key: "used", label: t("nav.campaigns"), width: 110, className: "num",
      value: (x) => usedBy(x.id),
      render: (x) => <span className="faint">{usedBy(x.id)}</span>,
    },
    {
      key: "created", label: "", width: 120, sortable: false,
      render: (x) => <span className="faint">{fmtTime(x.created_at)}</span>,
    },
    {
      key: "actions", label: "", sortable: false, className: "actions",
      render: (x) => (
        <>
          <button className="btn sm ghost" onClick={() => setEditing(x)}>
            {t("common.edit")}
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
        title={t("templates.title")}
        sub={t("templates.sub")}
        actions={
          <button className="btn accent" onClick={() => setCreating(true)}>
            {t("templates.new")}
          </button>
        }
      />

      <div className="card">
        <DataTable columns={columns} rows={snapshot.templates} rowKey={(x) => x.id}
                   empty={t("templates.empty")} />
      </div>

      <p className="field-hint" style={{ marginTop: 10 }}>{t("templates.note")}</p>

      {(creating || editing) && (
        <TemplateModal template={editing}
                       onClose={() => { setCreating(false); setEditing(null); }} />
      )}
      {removing && (
        <Confirm
          text={t("common.confirm_delete", { name: removing.name })}
          onCancel={() => setRemoving(null)}
          onConfirm={() => {
            const id = removing.id;
            setRemoving(null);
            void act(() => api.templates.remove(id));
          }}
        />
      )}
    </>
  );
}

function TemplateModal({ template, onClose }: {
  template: Template | null; onClose: () => void;
}) {
  const { t, act } = useStore();
  const [name, setName] = useState(template?.name ?? "");
  const [text, setText] = useState(template?.text ?? "");

  async function save() {
    await act(() => api.templates.save({ id: template?.id, name, text }));
    onClose();
  }

  return (
    <Modal
      wide
      title={template ? t("common.edit") : t("templates.new")}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>{t("common.cancel")}</button>
          <button className="btn accent" onClick={save} disabled={!name.trim()}>
            {t("common.save")}
          </button>
        </>
      }
    >
      <Field label={t("common.name")}>
        <input className="input" autoFocus value={name}
               onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label={t("templates.text")} hint={t("autoreply.operator_note")}>
        <textarea className="textarea" style={{ minHeight: 190 }} value={text}
                  onChange={(e) => setText(e.target.value)} />
      </Field>
    </Modal>
  );
}
