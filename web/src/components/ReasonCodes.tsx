import { useI18n } from "../i18n";

export function ReasonCodes({ codes }: { codes: string[] }) {
  const { t } = useI18n();
  if (!codes.length) return null;
  return <div className="reason-list" aria-label="Reason codes">
    {codes.map((code) => <div className="reason-item" key={code}>
      <code>{code}</code><span>{t(`reason.${code}`) === `reason.${code}` ? t("reason.default") : t(`reason.${code}`)}</span>
    </div>)}
  </div>;
}
