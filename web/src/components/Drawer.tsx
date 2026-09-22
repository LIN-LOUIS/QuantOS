import type { ReactNode } from "react";

export function Drawer({ title, open, onClose, children }: { title: string; open: boolean; onClose: () => void; children: ReactNode }) {
  if (!open) return null;
  return <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}>
    <aside className="drawer" role="dialog" aria-modal="true" aria-label={title}>
      <header><div><span className="eyebrow">AUDIT DETAIL</span><h2>{title}</h2></div>
        <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>×</button></header>
      <div className="drawer-content">{children}</div>
    </aside>
  </div>;
}
