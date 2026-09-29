import { useState } from "react";
import { Drawer } from "./Drawer";
import { useI18n } from "../i18n";

export function FeedbackPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { locale, t } = useI18n();
  const [rating, setRating] = useState<"useful" | "needs_work" | null>(null);
  const [notes, setNotes] = useState("");
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    const payload = { product: "QuantOS Public Preview", locale, usefulness: rating, feedback: notes || null };
    await navigator.clipboard?.writeText(JSON.stringify(payload, null, 2));
    setCopied(true);
  };
  return <Drawer title={t("feedback.title")} open={open} onClose={onClose}>
    <div className="feedback-panel"><p>{t("feedback.helpful")}</p><div className="feedback-rating">
      <button type="button" className={rating === "useful" ? "button-primary" : "button-secondary"} aria-pressed={rating === "useful"} onClick={() => setRating("useful")}>{t("feedback.yes")}</button>
      <button type="button" className={rating === "needs_work" ? "button-primary" : "button-secondary"} aria-pressed={rating === "needs_work"} onClick={() => setRating("needs_work")}>{t("feedback.no")}</button>
    </div><label>{t("feedback.notes")}<textarea value={notes} maxLength={1000} placeholder={t("feedback.placeholder")} onChange={(event) => setNotes(event.target.value)} /></label>
      <p className="muted">{t("feedback.privacy")}</p><button type="button" className="button-secondary" onClick={copy}>{copied ? t("feedback.copied") : t("feedback.copy")}</button>
    </div>
  </Drawer>;
}
