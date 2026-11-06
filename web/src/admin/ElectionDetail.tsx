import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import type { Election, ElectionStatus, Finding, Tally } from "../api/types";
import { Button, Card, ResultBadge, Spinner, StatusBadge, fmtDate } from "../ui";
import { LinkDeliveryCard, RosterCard } from "./RosterSteps";

const FLOW: [ElectionStatus, string][] = [
  ["DRAFT", "준비"],
  ["ROSTER_FROZEN", "명단 확정"],
  ["OPEN", "투표"],
  ["CLOSED", "마감"],
  ["TALLIED", "개표"],
  ["FINALIZED", "확정"],
];

export default function ElectionDetail() {
  const { electionId = "" } = useParams();
  const [e, setE] = useState<Election>();
  const [findings, setFindings] = useState<Finding[]>([]);
  const [tally, setTally] = useState<Tally>();
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: "ok" | "err"; text: string }>();

  const load = useCallback(async () => {
    const [el, fs, t] = await Promise.all([api.getElection(electionId), api.listFindings(electionId), api.getTally(electionId)]);
    setE(el);
    setFindings(fs);
    setTally(t);
  }, [electionId]);

  useEffect(() => {
    load();
  }, [load]);

  // the creation check (Core reads the pinned node) finishes on its own; refresh until it does
  const pending = e?.creation_check === "PENDING";
  useEffect(() => {
    if (!pending) return;
    const t = setInterval(load, 1500);
    return () => clearInterval(t);
  }, [pending, load]);

  async function run(fn: () => Promise<unknown>, ok: string) {
    setBusy(true);
    setMsg(undefined);
    try {
      await fn();
      setMsg({ tone: "ok", text: ok });
      await load();
    } catch (err) {
      setMsg({ tone: "err", text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  }

  if (!e)
    return (
      <div className="flex justify-center py-24 text-primary">
        <Spinner className="h-8 w-8" />
      </div>
    );

  const stepIdx = FLOW.findIndex(([s]) => s === e.status);
  const showResults = (e.status === "TALLIED" || e.status === "FINALIZED") && tally;

  return (
    <>
      <Link to="/admin" className="text-[14px] text-ink-3 hover:text-ink">
        ‹ 선거 목록
      </Link>
      <div className="mt-3 flex flex-wrap items-start justify-between gap-4">
        <div>
          <StatusBadge status={e.status} />
          <h1 className="mt-2 text-[22px] font-bold sm:text-[26px]">{e.title}</h1>
          <p className="mt-1 text-[15px] text-ink-3">
            {fmtDate(e.window_start)} ~ {fmtDate(e.window_end)}
          </p>
        </div>
        {e.status === "OPEN" && api.capabilities.mode === "mock" && (
          <a href={`/vote/${e.election_id}#t=demo-token`} target="_blank" rel="noreferrer">
            <Button variant="secondary">투표 화면 미리보기 ↗</Button>
          </a>
        )}
      </div>

      {/* progress */}
      <Card className="mt-6 p-4 sm:p-6">
        <ol className="flex items-center">
          {FLOW.map(([s, label], i) => {
            const done = i < stepIdx;
            const cur = i === stepIdx;
            return (
              <li key={s} className="flex flex-1 items-center last:flex-none">
                <span className="flex flex-col items-center gap-2">
                  <span
                    className={`flex h-7 w-7 items-center justify-center rounded-full text-[13px] font-bold sm:h-9 sm:w-9 sm:text-[14px] ${
                      cur ? "bg-primary text-white ring-4 ring-primary-soft" : done ? "bg-primary text-white" : "bg-surface text-ink-4"
                    }`}
                  >
                    {done ? "✓" : i + 1}
                  </span>
                  <span className={`whitespace-nowrap text-[11px] font-semibold sm:text-[13px] ${cur ? "text-primary" : done ? "text-ink-2" : "text-ink-4"}`}>{label}</span>
                </span>
                {i < FLOW.length - 1 && <span className={`mx-1 mb-6 h-0.5 sm:mx-2 flex-1 ${i < stepIdx ? "bg-primary" : "bg-line"}`} />}
              </li>
            );
          })}
        </ol>
      </Card>

      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* next action */}
        <Card className="p-6 lg:col-span-1">
          <h2 className="text-[17px] font-bold">다음 단계</h2>
          <div className="mt-4">
            <NextAction e={e} busy={busy} run={run} />
          </div>
          {msg && <p className={`mt-4 rounded-xl p-3 text-[14px] ${msg.tone === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>{msg.text}</p>}
        </Card>

        {/* counters */}
        <Card className="p-6 lg:col-span-2">
          <div className="flex items-baseline justify-between">
            <h2 className="text-[17px] font-bold">진행 현황</h2>
            <span className="text-[13px] text-ink-4">체크포인트 {e.counters.as_of_checkpoint} 기준</span>
          </div>
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Metric label="유권자" value={e.voter_count} />
            <Metric label="유효 링크" value={e.counters.tokens_active} />
            <Metric label="사용된 링크" value={e.counters.tokens_used} />
            <Metric label="접수된 투표" value={e.counters.ballots_received} highlight />
          </div>
          <p className="mt-4 text-[13px] text-ink-4">
            집계는 실시간이 아니라 체크포인트마다 갱신돼요. 투표 시각이 드러나지 않게 하기 위해서예요.
          </p>
        </Card>
      </div>

      {(e.status === "DRAFT" || e.status === "ROSTER_FROZEN") && <RosterCard e={e} onChange={load} />}
      {(e.status === "ROSTER_FROZEN" || e.status === "OPEN") && <LinkDeliveryCard e={e} onChange={load} />}

      {showResults && (
        <Card className="mt-4 p-6">
          <div className="flex items-baseline justify-between">
            <h2 className="text-[17px] font-bold">개표 결과</h2>
            <span className="text-[13px] text-ink-4">
              총 {tally.ballot_count}표 · 무효 {tally.invalid_ballots}표
            </span>
          </div>
          <div className="mt-5 space-y-4">
            {[...tally.results]
              .sort((a, b) => b.votes - a.votes)
              .map((r, i) => {
                const c = e.candidates.find((x) => x.candidate_id === r.candidate_id);
                const pct = tally.ballot_count ? (r.votes / tally.ballot_count) * 100 : 0;
                return (
                  <div key={r.candidate_id}>
                    <div className="flex justify-between text-[15px]">
                      <span className="font-semibold">
                        {c?.name}
                        {i === 0 && <span className="ml-2 text-[13px] text-primary">최다 득표</span>}
                      </span>
                      <span className="text-ink-3">
                        <b className="text-ink">{r.votes}표</b> ({pct.toFixed(1)}%)
                      </span>
                    </div>
                    <div className="mt-2 h-3 overflow-hidden rounded-full bg-surface">
                      <div className={`h-full rounded-full ${i === 0 ? "bg-primary" : "bg-[#90c2ff]"}`} style={{ width: `${pct}%` }} />
                    </div>
                  </div>
                );
              })}
          </div>
          <p className="mono mt-5 break-all text-[12px] text-ink-4">result_hash {tally.result_hash}</p>
        </Card>
      )}

      {/* rule engine findings */}
      <Card className="mt-4 overflow-hidden">
        <div className="flex items-baseline justify-between p-6 pb-4">
          <h2 className="text-[17px] font-bold">무결성 검사 결과</h2>
          <span className="text-[13px] text-ink-4">규칙 엔진 R1–R5 · 최신 체크포인트</span>
        </div>
        {findings.length === 0 ? (
          <p className="px-6 pb-6 text-[14px] text-ink-4">아직 검사 결과가 없어요. 투표가 시작되면 체크포인트마다 검사해요.</p>
        ) : (
          <div className="overflow-x-auto">
          <table className="w-full min-w-[560px] text-left text-[14px]">
            <thead className="border-y border-line bg-surface text-[13px] text-ink-3">
              <tr>
                <th className="px-6 py-3 font-semibold">규칙</th>
                <th className="py-3 font-semibold">결과</th>
                <th className="py-3 font-semibold">체크포인트</th>
                <th className="px-6 py-3 font-semibold">내용</th>
              </tr>
            </thead>
            <tbody>
              {findings.map((f) => (
                <tr key={f.finding_id} className={`border-b border-line last:border-0 ${f.result === "FAIL" ? "bg-danger-soft/40" : ""}`}>
                  <td className="px-6 py-3.5 font-bold">{f.rule_id}</td>
                  <td className="py-3.5">
                    <ResultBadge result={f.result} />
                    {f.confirmed_issue && <span className="ml-2 text-[12px] font-bold text-danger">문제 확정</span>}
                  </td>
                  <td className="py-3.5 text-ink-3">{f.checkpoint}</td>
                  <td className="px-6 py-3.5 text-ink-2">{f.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </Card>
    </>
  );
}

function NextAction({ e, busy, run }: { e: Election; busy: boolean; run: (fn: () => Promise<unknown>, ok: string) => void }) {
  const id = e.election_id;

  switch (e.status) {
    case "DRAFT":
      return (
        <div className="space-y-3">
          <p className="text-[14px] text-ink-3">아래에서 유권자 명단을 올린 뒤 확정하세요. 확정 후에는 기관 승인 없이 명단을 바꿀 수 없어요.</p>
          {e.creation_check !== "PASSED" && (
            <p className={`flex items-center gap-2 rounded-xl p-3 text-[13px] ${e.creation_check === "PENDING" ? "bg-surface text-ink-3" : "bg-danger-soft text-danger"}`}>
              {e.creation_check === "PENDING" ? (
                <>
                  <Spinner className="h-4 w-4" /> 선거 생성 검사 중이에요 (블록체인에 기록된 설정과 대조)
                </>
              ) : (
                "생성 검사 불일치: 이 선거는 쓸 수 없어요"
              )}
            </p>
          )}
          <Button className="w-full" disabled={busy || e.voter_count === 0 || e.creation_check !== "PASSED"} onClick={() => run(() => api.freezeRoster(id), "명단을 확정했어요")}>
            명단 확정 ({e.voter_count}명)
          </Button>
        </div>
      );
    case "ROSTER_FROZEN":
      return (
        <div className="space-y-3">
          <p className="text-[14px] text-ink-3">아래에서 투표 링크를 발급해 각 유권자에게 전달하세요.</p>
          {api.capabilities.manualOpenClose ? (
            <Button className="w-full" disabled={busy} onClick={() => run(() => api.openElection(id), "투표를 시작했어요")}>
              투표 시작 (데모)
            </Button>
          ) : (
            <p className="rounded-xl bg-surface p-3 text-[13px] text-ink-3">시작 시각({fmtDate(e.window_start)})이 되면 투표가 자동으로 열려요.</p>
          )}
        </div>
      );
    case "OPEN":
      return (
        <div className="space-y-3">
          <p className="text-[14px] text-ink-3">투표가 진행 중이에요. 종료 시각({fmtDate(e.window_end)})이 되면 자동으로 마감돼요.</p>
          {api.capabilities.manualOpenClose && (
            <Button variant="ghost" className="w-full" disabled={busy} onClick={() => run(() => api.closeElection(id), "투표를 마감했어요")}>
              투표 마감 (데모)
            </Button>
          )}
        </div>
      );
    case "CLOSED":
      return (
        <div className="space-y-3">
          <p className="text-[14px] text-ink-3">선거 키를 공개하고 개표해요. 결과는 블록체인에 기록돼요.</p>
          {api.capabilities.tally ? (
            <Button className="w-full" disabled={busy} onClick={() => run(() => api.tally(id), "개표를 마쳤어요")}>
              개표하기
            </Button>
          ) : (
            <p className="rounded-xl bg-surface p-3 text-[13px] text-ink-3">개표 기능은 Core에 곧 추가돼요 (11-09).</p>
          )}
        </div>
      );
    case "TALLIED":
      return (
        <div className="space-y-3">
          <p className="text-[14px] text-ink-3">모든 검사가 통과했거나 검토가 끝나야 결과를 확정할 수 있어요.</p>
          {api.capabilities.finalize ? (
            <Button className="w-full" disabled={busy} onClick={() => run(() => api.finalize(id), api.capabilities.mode === "mock" ? "결과를 확정했어요" : "결과 확정을 요청했어요. 원장에 기록되면 확정 상태로 바뀌어요.")}>
              결과 확정
            </Button>
          ) : (
            <p className="rounded-xl bg-surface p-3 text-[13px] text-ink-3">결과 확정 기능은 Core에 곧 추가돼요 (11-09).</p>
          )}
        </div>
      );
    case "FINALIZED":
      return <p className="text-[14px] text-ink-3">결과가 확정됐어요. 더 할 일이 없어요.</p>;
    default:
      return <p className="text-[14px] text-ink-3">무효 처리된 선거예요.</p>;
  }
}

function Metric({ label, value, highlight }: { label: string; value: number; highlight?: boolean }) {
  return (
    <div className={`rounded-xl p-4 ${highlight ? "bg-primary-soft" : "bg-surface"}`}>
      <div className="text-[13px] text-ink-3">{label}</div>
      <div className={`mt-1 text-[22px] font-bold ${highlight ? "text-primary" : ""}`}>{value}</div>
    </div>
  );
}
