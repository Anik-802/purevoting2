import type { ButtonHTMLAttributes, ReactNode } from "react";
import type { ElectionStatus, RuleResult } from "../api/types";

type Variant = "primary" | "secondary" | "ghost" | "danger";

const variants: Record<Variant, string> = {
  primary: "bg-primary text-white hover:bg-primary-hover disabled:bg-primary/40",
  secondary: "bg-primary-soft text-primary hover:bg-[#c9e2ff] disabled:opacity-50",
  ghost: "bg-surface text-ink-3 hover:bg-line disabled:opacity-50",
  danger: "bg-danger text-white hover:bg-[#e42939] disabled:opacity-50",
};

export function Button({
  variant = "primary",
  size = "md",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "md" | "lg" }) {
  const sz = size === "lg" ? "h-14 text-[17px] rounded-2xl px-6" : "h-10 text-[15px] rounded-xl px-4";
  return (
    <button
      className={`inline-flex items-center justify-center gap-2 font-semibold transition-colors disabled:cursor-not-allowed ${sz} ${variants[variant]} ${className}`}
      {...props}
    />
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`rounded-2xl border border-line bg-white ${className}`}>{children}</div>;
}

const tones = {
  blue: "bg-primary-soft text-primary",
  green: "bg-ok-soft text-ok",
  red: "bg-danger-soft text-danger",
  orange: "bg-warn-soft text-warn",
  grey: "bg-surface text-ink-3",
} as const;

export function Badge({ tone = "grey", children }: { tone?: keyof typeof tones; children: ReactNode }) {
  return <span className={`inline-flex items-center rounded-md px-2 py-0.5 text-[13px] font-semibold ${tones[tone]}`}>{children}</span>;
}

export const statusLabel: Record<ElectionStatus, [string, keyof typeof tones]> = {
  DRAFT: ["준비 중", "grey"],
  ROSTER_FROZEN: ["명단 확정", "blue"],
  OPEN: ["투표 진행 중", "green"],
  CLOSED: ["투표 마감", "orange"],
  TALLIED: ["개표 완료", "blue"],
  FINALIZED: ["결과 확정", "blue"],
  VOIDED: ["무효", "red"],
};

export function StatusBadge({ status }: { status: ElectionStatus }) {
  const [label, tone] = statusLabel[status];
  return <Badge tone={tone}>{label}</Badge>;
}

export function ResultBadge({ result }: { result: RuleResult }) {
  const map = { PASS: ["통과", "green"], FAIL: ["이상 탐지", "red"], INCONCLUSIVE: ["판단 보류", "orange"] } as const;
  const [label, tone] = map[result];
  return <Badge tone={tone}>{label}</Badge>;
}

export function Spinner({ className = "h-5 w-5" }: { className?: string }) {
  return <span className={`inline-block animate-spin rounded-full border-2 border-current border-t-transparent ${className}`} />;
}

export function BottomSheet({ open, onClose, children }: { open: boolean; onClose: () => void; children: ReactNode }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/40 sm:items-center" onClick={onClose}>
      <div
        className="w-full max-w-md rounded-t-3xl bg-white p-6 pb-8 sm:rounded-3xl"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        {children}
      </div>
    </div>
  );
}

export const fmtDate = (iso: string) =>
  new Date(iso).toLocaleString("ko-KR", { month: "long", day: "numeric", weekday: "short", hour: "2-digit", minute: "2-digit" });
