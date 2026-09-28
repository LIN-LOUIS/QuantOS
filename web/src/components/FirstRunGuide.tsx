import { useState } from "react";
import { useI18n } from "../i18n";

const STORAGE_KEY = "quantos.firstRunGuide.dismissed";

export function FirstRunGuide() {
  const { t } = useI18n();
  const [visible, setVisible] = useState(() => window.localStorage.getItem(STORAGE_KEY) !== "true");
  if (!visible) return null;
  const dismiss = () => { window.localStorage.setItem(STORAGE_KEY, "true"); setVisible(false); };
  return <section className="first-run-guide" aria-label={t("guide.title")}>
    <div className="guide-heading"><strong>{t("guide.title")}</strong><button type="button" className="button-quiet" onClick={dismiss}>{t("guide.dismiss")}</button></div>
    <div className="guide-steps">
      <div><b>{t("guide.ask")}</b><span>{t("guide.askText")}</span></div>
      <div><b>{t("guide.inspect")}</b><span>{t("guide.inspectText")}</span></div>
      <div><b>{t("guide.replay")}</b><span>{t("guide.replayText")}</span></div>
    </div>
  </section>;
}
