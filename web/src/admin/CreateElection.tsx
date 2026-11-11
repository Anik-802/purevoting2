import { useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Button, Card } from "../ui";

const toLocal = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);

// D-10: the time bucket is fixed at one minute (not chosen by the admin); datetime-local inputs have minute precision,
// so the window is always on a bucket boundary
const BUCKET_SECONDS = 60;

export default function CreateElection() {
  const nav = useNavigate();
  const [title, setTitle] = useState("");
  // next quarter hour after one hour from now
  const firstStart = Math.ceil((Date.now() + 3_600_000) / 900_000) * 900_000;
  const [start, setStart] = useState(toLocal(new Date(firstStart)));
  // demo configuration: 10-minute window
  const [end, setEnd] = useState(toLocal(new Date(firstStart + 600_000)));
  const [candidates, setCandidates] = useState([{ name: "", description: "" }, { name: "", description: "" }]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // one Idempotency-Key per form content: a lost answer or a double click never creates a second election
  const idemKey = useMemo(() => crypto.randomUUID(), [title, start, end, candidates]);

  // the first missing item, shown under the disabled button
  const blankCandidate = candidates.findIndex((c) => !c.name.trim());
  const missing = !title.trim()
    ? "선거 이름을 입력하세요."
    : !start || !end || !(new Date(end) > new Date(start))
      ? "투표 종료는 투표 시작보다 늦어야 해요."
      : blankCandidate >= 0
        ? `후보 ${blankCandidate + 1}의 이름을 입력하거나, 쓰지 않는 줄은 삭제하세요.`
        : "";
  const valid = !missing;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const el = await api.createElection({
        title: title.trim(),
        time_bucket_seconds: BUCKET_SECONDS,
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
                // flex-[2] (basis 0) instead of the input's w-full, which squeezed the name field to zero width
                <input className={`${input} order-last min-w-0 basis-full sm:order-none sm:flex-[2]`} placeholder="공약 한 줄 (선택)" value={c.description} onChange={(e) => set(i, "description", e.target.value)} />
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
      {missing && <p className="mt-3 text-right text-[14px] text-ink-3">{missing}</p>}
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
