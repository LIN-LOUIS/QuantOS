import type { ReactNode } from "react";
import { useI18n } from "../i18n";

export function Drawer({ title, open, onClose, children }: { title: string; open: boolean; onClose: () => void; children: ReactNode }) {
  const { t } = useI18n();
  if (!open) return null;
  return <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}>
    <aside className="drawer" role="dialog" aria-modal="true" aria-label={title}>
      <header><div><span className="eyebrow">{t("common.auditDetail")}</span><h2>{title}</h2></div>
        <button type="button" className="icon-button" aria-label={t("common.close")} onClick={onClose}>×</button></header>
      <div className="drawer-content">{children}</div>
    </aside>
  </div>;
}
