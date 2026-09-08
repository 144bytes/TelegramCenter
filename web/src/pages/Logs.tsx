import { useState } from "react";
import { useStore } from "../state";
import { PageHead, Switch, useAutoScroll } from "../components/ui";

const LEVELS = ["ALL", "INFO", "WARNING", "ERROR"] as const;

export function Logs() {
  const { logs, t, clearLogs, snapshot } = useStore();
  const [level, setLevel] = useState<(typeof LEVELS)[number]>("ALL");
  const [autoscroll, setAutoscroll] = useState(true);

  const showTime = snapshot?.settings.general.show_log_time ?? true;
  const visible = logs.filter((l) => {
    if (level === "ALL") return true;
    if (level === "INFO") return l.level === "INFO";
    if (level === "WARNING") return l.level === "WARNING" || l.level === "ERROR";
    return l.level === "ERROR";
  });

  const ref = useAutoScroll<HTMLDivElement>(visible.length, autoscroll);

  return (
    <>
      <PageHead
        title={t("logs.title")}
        sub={t("logs.sub")}
        actions={
          <>
            <select className="select" style={{ width: 130 }} value={level}
                    onChange={(e) => setLevel(e.target.value as typeof level)}>
              {LEVELS.map((l) => (
                <option key={l} value={l}>{l === "ALL" ? t("common.all") : l}</option>
              ))}
            </select>
            <Switch checked={autoscroll} onChange={setAutoscroll}
                    label={t("logs.autoscroll")} />
            <button className="btn" onClick={clearLogs}>{t("logs.clear")}</button>
          </>
        }
      />
      <div className="log-view" ref={ref}>
        {visible.map((line, i) => (
          <div className={`log-line ${line.level}`} key={i}>
            {showTime && <span className="log-ts">{line.ts} </span>}
            {line.module && <span className="log-mod">[{line.module}] </span>}
            {line.message}
          </div>
        ))}
        {!visible.length && <div className="faint">—</div>}
      </div>
    </>
  );
}
