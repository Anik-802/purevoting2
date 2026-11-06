import { useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Button, Card } from "../ui";

const toLocal = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);

export default function CreateElection() {
  const nav = useNavigate();
  const [title, setTitle] = useState("");
  const [bucket, setBucket] = useState(60);
  // next quarter hour after one hour from now: aligned for every bucket choice
  const firstStart = Math.ceil((Date.now() + 3_600_000) / 900_000) * 900_000;
  const [start, setStart] = useState(toLocal(new Date(firstStart)));
  // demo configuration: 10-minute window (D-10: 100 voters keep ≥ 10 expected voters per 60 s bucket)
  const [end, setEnd] = useState(toLocal(new Date(firstStart + 600_000)));
  const [candidates, setCandidates] = useState([{ name: "", description: "" }, { name: "", description: "" }]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // one Idempotency-Key per form content: a lost answer or a double click never creates a second election
  const idemKey = useMemo(() => crypto.randomUUID(), [title, bucket, start, end, candidates]);

  // D-10 / contracts README: the window must start and end on a time-bucket boundary
  const aligned = (v: string) => (new Date(v).getTime() / 1000) % bucket === 0;
  const misaligned = !aligned(start) || !aligned(end);
  const valid = title.trim() && candidates.every((c) => c.name.trim()) && new Date(end) > new Date(start) && !misaligned;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const el = await api.createElection({
        title: title.trim(),
        time_bucket_seconds: bucket,
        window_start: new Date(start).toISOString(),
        window_end: new Date(end).toISOString(),
        candidates: candidates.map((c) => ({ name: c.name.trim(), description: c.description.trim() || undefined })),
      }, idemKey);
      nav(`/admin/elections/${el.election_id}`);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  const input = "h-12 w-full rounded-xl border border-line px-4 text-[15px] outline-none focus:border-primary";
  const set = (i: number, k: "name" | "description", v: string) => setCandidates((cs) => cs.map((c, j) => (j === i ? { ...c, [k]: v } : c)));

  return (
    <form onSubmit={submit} className="mx-auto max-w-2xl">
      <h1 className="text-[26px] font-bold">새 선거 만들기</h1>
      <p className="mt-1 text-[15px] text-ink-3">기본 정보와 후보를 입력하세요. 만든 뒤 명단을 등록할 수 있어요.</p>

      <Card className="mt-6 space-y-5 p-6">
        <Field label="선거 이름">
          <input className={input} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="예: 2026 총학생회장 선거" />
        </Field>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="투표 시작">
            <input type="datetime-local" className={input} value={start} onChange={(e) => setStart(e.target.value)} />
          </Field>
          <Field label="투표 종료">
            <input type="datetime-local" className={input} value={end} onChange={(e) => setEnd(e.target.value)} />
          </Field>
        </div>
        <Field label="시간 구간 (투표 시각을 이 단위로만 기록)">
          <select className={input} value={bucket} onChange={(e) => setBucket(Number(e.target.value))}>
            <option value={60}>60초 (시연 설정)</option>
            <option value={300}>5분</option>
            <option value={900}>15분 (평가 설정)</option>
          </select>
        </Field>
        <p className="-mt-2 text-[13px] text-ink-4">구간마다 예상 투표자가 10명 이상이어야 명단을 확정할 수 있어요. 기간이 길면 구간을 늘리세요.</p>
        {misaligned && (
          <p className="rounded-xl bg-warn-soft p-3 text-[13px] text-warn">
            투표 시작·종료 시각은 시간 구간({bucket / 60}분) 단위로 맞춰야 해요. 예: {bucket === 900 ? "10:00, 10:15, 10:30" : "10:00, 10:05, 10:10"}
          </p>
        )}
      </Card>

      <Card className="mt-4 p-6">
        <div className="flex items-center justify-between">
          <h2 className="text-[17px] font-bold">후보</h2>
          <Button type="button" variant="secondary" onClick={() => setCandidates((cs) => [...cs, { name: "", description: "" }])}>
            + 후보 추가
          </Button>
        </div>
        <div className="mt-4 space-y-3">
          {candidates.map((c, i) => (
            <div key={i} className="flex flex-wrap gap-3 sm:flex-nowrap">
              <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl bg-primary-soft text-[15px] font-bold text-primary">
                {i + 1}
              </span>
              <input className={`${input} min-w-0 flex-1`} placeholder="이름" value={c.name} onChange={(e) => set(i, "name", e.target.value)} />
              {/* core-api Candidate has no description yet: pledges exist only in the mock */}
              {api.capabilities.mode === "mock" && (
                <input className={`${input} order-last sm:order-none`} placeholder="공약 한 줄 (선택)" value={c.description} onChange={(e) => set(i, "description", e.target.value)} />
              )}
              <Button
                type="button"
                variant="ghost"
                className="h-12 shrink-0"
                disabled={candidates.length === 1}
                onClick={() => setCandidates((cs) => cs.filter((_, j) => j !== i))}
              >
                삭제
              </Button>
            </div>
          ))}
        </div>
      </Card>

      {error && <p className="mt-3 text-[14px] text-danger">{error}</p>}
      <div className="mt-6 flex justify-end gap-2">
        <Button type="button" variant="ghost" onClick={() => nav("/admin")}>
          취소
        </Button>
        <Button type="submit" disabled={!valid || busy}>
          선거 만들기
        </Button>
      </div>
    </form>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-2 block text-[14px] font-semibold text-ink-3">{label}</span>
      {children}
    </label>
  );
}
