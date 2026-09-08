import { useMemo, useState } from "react";
import { useStore } from "../state";
import {
  ActionButton, PageHead, Select, Switch, useAutoScroll,
} from "../components/ui";

const LEVELS = ["ALL", "INFO", "WARNING", "ERROR"] as const;

/** Lines with no module at all. Given a value of its own so "without a
 *  module" can be chosen deliberately rather than only by accident. */
const NO_MODULE = "\u0000none";

export function Logs() {
  const { logs, t, clearLogs, snapshot } = useStore();
  const [level, setLevel] = useState<(typeof LEVELS)[number]>("ALL");
  const [module, setModule] = useState("ALL");
  const [autoscroll, setAutoscroll] = useState(true);

  const showTime = snapshot?.settings.general.show_log_time ?? true;
  const logsDir = snapshot?.paths?.logs ?? "";

  // Taken from the lines actually in hand rather than from a hard-coded
  // list: a module that stops logging disappears from the menu instead of
  // offering a filter that can only ever show nothing, and one added to the
  // backend turns up here without anybody remembering to add it.
  const modules = useMemo(() => {
    const seen = new Set<string>();
    for (const line of logs) seen.add(line.module || NO_MODULE);
    return [...seen].sort((a, b) => a.localeCompare(b));
  }, [logs]);

  const visible = logs.filter((l) => {
    if (module !== "ALL" && (l.module || NO_MODULE) !== module) return false;
    if (level === "ALL") return true;
    if (level === "INFO") return l.level === "INFO";
    if (level === "WARNING") return l.level === "WARNING" || l.level === "ERROR";
    return l.level === "ERROR";
  });

  const ref = useAutoScroll<HTMLDivElement>(visible.length, autoscroll);

  return (
    <>
      <PageHead title={t("logs.title")}
                sub={logsDir ? t("logs.sub_files", { dir: logsDir }) : t("logs.sub")} />
      <div className="filters">
        <Select width="pick" value={module} title={t("logs.module")} onChange={setModule}
                options={[
                  { value: "ALL", label: `${t("logs.module")}: ${t("common.all")}` },
                  ...modules.map((m) => ({
                    value: m, label: m === NO_MODULE ? t("logs.module_none") : m })),
                ]} />
        <Select width="pick" value={level} title={t("logs.level")}
                onChange={(v) => setLevel(v as typeof level)}
                options={LEVELS.map((l) => ({
                  value: l, label: `${t("logs.level")}: ${t(`logs.level.${l}`)}` }))} />
        <Switch checked={autoscroll} onChange={setAutoscroll} label={t("logs.autoscroll")} />
        <span className="spacer" />
        <ActionButton onClick={clearLogs}>{t("logs.clear")}</ActionButton>
      </div>
      <div className="log-view" ref={ref}>
        {visible.map((line, i) => (
          <div className={`log-line ${line.level}`} key={i}>
            {showTime && <span className="log-ts">{line.ts} </span>}
            {line.module && (
              // Clicking a tag filters by it: the usual reason to notice a
              // module is wanting to see only that module.
              <span className="log-mod" role="button" title={t("logs.only", { module: line.module })}
                    onClick={() => setModule(line.module as string)}>
                [{line.module}]{" "}
              </span>
            )}
            {line.message}
          </div>
        ))}
        {!visible.length && <div className="faint">—</div>}
      </div>
    </>
  );
}
