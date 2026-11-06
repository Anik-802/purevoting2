import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { Election } from "../api/types";
import { Button, Card, Spinner, StatusBadge, fmtDate } from "../ui";

function dDay(e: Election) {
  const ms = new Date(e.window_start).getTime() - Date.now();
  if (e.status !== "DRAFT" && e.status !== "ROSTER_FROZEN") return null;
  if (ms <= 0) return null;
  return `D-${Math.ceil(ms / 86_400_000)}`;
}

export default function ElectionList() {
  const [items, setItems] = useState<Election[]>();
  useEffect(() => {
    api.listElections().then(setItems);
  }, []);

  if (!items)
    return (
      <div className="flex justify-center py-24 text-primary">
        <Spinner className="h-8 w-8" />
      </div>
    );

  const open = items.filter((e) => e.status === "OPEN").length;
  const pending = items.filter((e) => e.status === "DRAFT" || e.status === "ROSTER_FROZEN").length;
  const done = items.filter((e) => e.status === "TALLIED" || e.status === "FINALIZED").length;

  return (
    <>
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-[26px] font-bold">선거 관리</h1>
          <p className="mt-1 text-[15px] text-ink-3">선거를 만들고, 진행 상황과 검사 결과를 확인하세요.</p>
        </div>
        <Link to="/admin/new">
          <Button>+ 새 선거</Button>
        </Link>
      </div>

      {/* summary tiles (NEC quick-menu style) */}
      <div className="mt-6 grid grid-cols-1 gap-3 sm:grid-cols-3 sm:gap-4">
        <Tile icon="🗳️" label="진행 중인 선거" value={open} />
        <Tile icon="📝" label="준비 중인 선거" value={pending} />
        <Tile icon="📊" label="개표 완료" value={done} />
      </div>

      <div className="mt-8 space-y-3">
        {items.map((e) => {
          const d = dDay(e);
          return (
            <Link key={e.election_id} to={`/admin/elections/${e.election_id}`} className="block">
              <Card className="flex items-center gap-6 p-6 transition-shadow hover:shadow-[0_4px_20px_rgba(0,27,55,0.08)]">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <StatusBadge status={e.status} />
                    {d && <span className="rounded-full bg-navy px-2.5 py-0.5 text-[12px] font-bold text-white">{d}</span>}
                  </div>
                  <h2 className="mt-2 truncate text-[18px] font-bold">{e.title}</h2>
                  <p className="mt-1 text-[14px] text-ink-3">
                    {fmtDate(e.window_start)} ~ {fmtDate(e.window_end)}
                  </p>
                </div>
                <Stat label="유권자" value={`${e.voter_count}명`} />
                <Stat label="접수된 투표" value={`${e.counters.ballots_received}표`} />
                <span className="text-xl text-ink-4">›</span>
              </Card>
            </Link>
          );
        })}
      </div>
    </>
  );
}

function Tile({ icon, label, value }: { icon: string; label: string; value: number }) {
  return (
    <Card className="flex items-center gap-4 p-5">
      <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-primary-soft text-2xl">{icon}</span>
      <span>
        <span className="block text-[14px] text-ink-3">{label}</span>
        <span className="block text-[22px] font-bold">{value}</span>
      </span>
    </Card>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="hidden w-28 text-right sm:block">
      <div className="text-[13px] text-ink-4">{label}</div>
      <div className="text-[17px] font-bold">{value}</div>
    </div>
  );
}
