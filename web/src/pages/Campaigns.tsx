import { useState } from "react";
import { api } from "../api";
import { useStore } from "../state";
import { CampaignModal } from "../components/CampaignModal";
import {
  Confirm, DataTable, IssueDot, Modal, PageHead, Status, fmtTime,
} from "../components/ui";
import type { Column } from "../components/ui";
import type { Campaign as CampaignType } from "../types";
import { CampaignActions } from "./Accounts";

export function Campaigns() {
  const { snapshot, t, act } = useStore();
  const [editing, setEditing] = useState<CampaignType | null>(null);
  const [creating, setCreating] = useState(false);
  const [removing, setRemoving] = useState<CampaignType | null>(null);
  const [details, setDetails] = useState<CampaignType | null>(null);

  if (!snapshot) return null;

  const accountName = (id: string) =>
    snapshot.accounts.find((a) => a.id === id)?.handle ?? "—";

  const columns: Column<CampaignType>[] = [
    {
      key: "dot", label: "", width: 26, sortable: false,
      render: (c) => <IssueDot issues={c.issues} state={c.effective} />,
    },
    {
      key: "name", label: t("common.name"),
      value: (c) => c.name,
      render: (c) => (
        <button className="btn ghost sm" style={{ padding: 0 }}
                onClick={() => setDetails(c)}>{c.name}</button>
      ),
    },
    {
      key: "account", label: t("campaigns.account"), width: 170,
      value: (c) => accountName(c.account_id),
      render: (c) => <span className="muted">{accountName(c.account_id)}</span>,
    },
    {
      key: "status", label: t("common.status"), width: 150,
      value: (c) => c.effective,
      render: (c) => <Status state={c.effective} issues={[]} />,
    },
    {
      key: "progress", label: t("campaigns.progress"), width: 120,
      className: "num",
      value: (c) => (c.is_recurring ? c.sent_total : c.sent_count),
      render: (c) => (
        <>
          {/* a repeating campaign has no meaningful "of N": it keeps going,
              so show what it has actually sent rather than a share of one pass */}
          {c.is_recurring ? c.sent_total : `${c.sent_count}/${c.total_count}`}
          {c.failed_count > 0 && <span className="err-text"> · {c.failed_count}</span>}
        </>
      ),
    },
    {
      key: "next", label: t("campaigns.next_run"), width: 140,
      value: (c) => c.next_run_at ?? "",
      render: (c) => <span className="muted">{fmtTime(c.next_run_at)}</span>,
    },
    {
      key: "actions", label: "", sortable: false, className: "actions",
      render: (c) => (
        <>
          <CampaignActions campaign={c} />
          <button className="btn sm ghost" onClick={() => setEditing(c)}>
            {t("common.edit")}
          </button>
          <button className="btn sm danger" onClick={() => setRemoving(c)}>
            {t("common.delete")}
          </button>
        </>
      ),
    },
  ];

  return (
    <>
      <PageHead
        title={t("campaigns.title")}
        sub={t("campaigns.sub")}
        actions={
          <button className="btn accent" onClick={() => setCreating(true)}>
            {t("campaigns.new")}
          </button>
        }
      />

      <div className="card">
        <DataTable
          columns={columns}
          rows={snapshot.campaigns}
          rowKey={(c) => c.id}
          empty={t("campaigns.empty")}
        />
      </div>

      {(creating || editing) && (
        <CampaignModal
          campaign={editing}
          onClose={() => { setCreating(false); setEditing(null); }}
        />
      )}
      {removing && (
        <Confirm
          text={t("common.confirm_delete", { name: removing.name })}
          onCancel={() => setRemoving(null)}
          onConfirm={() => {
            const id = removing.id;
            setRemoving(null);
            void act(() => api.campaigns.remove(id));
          }}
        />
      )}
      {details && <ResultsModal campaign={details} onClose={() => setDetails(null)} />}
    </>
  );
}

function ResultsModal({ campaign, onClose }: {
  campaign: CampaignType; onClose: () => void;
}) {
  const { snapshot, t, act } = useStore();
  const name = (id: string) => {
    const target = snapshot?.targets.find((x) => x.id === id);
    return target ? (target.title || `@${target.username}`) : id;
  };

  return (
    <Modal
      wide
      title={`${campaign.name} — ${t("campaigns.results")}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn left"
                  onClick={() => act(() => api.campaigns.reset(campaign.id))}>
            {t("campaigns.reset")}
          </button>
          <button className="btn"
                  onClick={() => act(() => api.campaigns.runNow(campaign.id))}>
            {t("campaigns.run_now")}
          </button>
          <button className="btn accent" onClick={onClose}>{t("common.close")}</button>
        </>
      }
    >
      {campaign.last_error && (
        <div className="form-error">{campaign.last_error}</div>
      )}
      <table className="table">
        <thead>
          <tr>
            <th className="static">{t("campaigns.targets")}</th>
            <th className="static" style={{ width: 120 }}>{t("common.status")}</th>
            <th className="static" style={{ width: 70 }}>{t("campaigns.attempts")}</th>
            <th className="static" style={{ width: 130 }}>{t("common.error")}</th>
          </tr>
        </thead>
        <tbody>
          {campaign.results.map((r) => (
            <tr key={r.target_id}>
              <td>{name(r.target_id)}</td>
              <td>
                <span className={`dot ${
                  r.status === "SENT" ? "ok"
                    : r.status === "FAILED" ? "err"
                      : r.status === "SKIPPED" ? "warn" : "idle"}`} />{" "}
                {t(`campaigns.result.${r.status}`)}
              </td>
              <td className="num muted">{r.attempts}</td>
              <td className="muted truncate" title={r.error ?? ""}>{r.error ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Modal>
  );
}
