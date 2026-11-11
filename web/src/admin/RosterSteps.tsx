import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Election, RosterVoter, VoterInput } from "../api/types";
import { Button, Card } from "../ui";

// ───────────── roster upload (DRAFT) ─────────────

type Row = VoterInput & { line: number; error?: string };

const SAMPLE = "external_ref,name,email\n20260001,김하늘,sky@example.ac.kr\n20260002,이바다,\n20260003,박구름,cloud@example.ac.kr\n";
const HEADERS: Record<string, keyof VoterInput> = {
  external_ref: "external_ref", 학번: "external_ref", 번호: "external_ref", id: "external_ref",
  name: "name", 이름: "name", 성명: "name",
  email: "email", 이메일: "email", 메일: "email",
};
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** RFC 4180-style CSV: quoted fields, doubled quotes, CRLF */
function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') (cell += '"'), i++;
      else if (ch === '"') quoted = false;
      else cell += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ",") row.push(cell), (cell = "");
    else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(cell), rows.push(row), (row = []), (cell = "");
    } else cell += ch;
  }
  if (cell || row.length) row.push(cell), rows.push(row);
  return rows.filter((r) => r.some((c) => c.trim()));
}

function toRows(text: string): Row[] {
  const table = parseCsv(text.replace(/^﻿/, ""));
  if (!table.length) return [];
  const head = table[0].map((h) => HEADERS[h.trim().toLowerCase()]);
  const hasHeader = head.includes("external_ref") && head.includes("name");
  const cols: (keyof VoterInput | undefined)[] = hasHeader ? head : ["external_ref", "name", "email"];
  const seen = new Set<string>();
  return table.slice(hasHeader ? 1 : 0).map((cells, i) => {
    const r: Row = { line: i + (hasHeader ? 2 : 1), external_ref: "", name: "" };
    cols.forEach((k, j) => k && (r[k] = (cells[j] ?? "").trim()));
    if (!r.email) delete r.email;
    if (!r.external_ref) r.error = "학번 없음";
    else if (r.external_ref.length > 64) r.error = "학번이 64자를 넘어요";
    else if (seen.has(r.external_ref)) r.error = "학번 중복";
    else if (!r.name) r.error = "이름 없음";
    else if (r.name.length > 100) r.error = "이름이 100자를 넘어요";
    else if (r.email && !EMAIL.test(r.email)) r.error = "이메일 형식 오류";
    seen.add(r.external_ref);
    return r;
  });
}

/** Excel in Korean Windows saves CSV as CP949; try UTF-8 first */
async function readText(file: File) {
  const buf = await file.arrayBuffer();
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(buf);
  } catch {
    return new TextDecoder("euc-kr").decode(buf);
  }
}

function download(name: string, text: string) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["﻿" + text], { type: "text/csv;charset=utf-8" }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 0);
}

export function RosterCard({ e, onChange }: { e: Election; onChange: () => void }) {
  const editable = e.status === "DRAFT";
  const [rows, setRows] = useState<Row[]>();
  const [fileName, setFileName] = useState("");
  const [paste, setPaste] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: "ok" | "err"; text: string }>();
  const [saved, setSaved] = useState<RosterVoter[]>();
  const [hash, setHash] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.readRoster(e.election_id).then(setSaved);
  }, [e.election_id, e.voter_count]);

  const errors = rows?.filter((r) => r.error) ?? [];

  async function onFile(f?: File) {
    if (!f) return;
    setFileName(f.name);
    setMsg(undefined);
    setRows(toRows(await readText(f)));
  }

  async function save() {
    if (!rows) return;
    setBusy(true);
    setMsg(undefined);
    try {
      const s = await api.replaceRoster(e.election_id, rows.map(({ external_ref, name, email }) => ({ external_ref, name, ...(email ? { email } : {}) })));
      setHash(s.roster_hash);
      setRows(undefined);
      setText("");
      setMsg({ tone: "ok", text: `명단 ${s.voter_count}명을 등록했어요` });
      onChange();
    } catch (err) {
      setMsg({ tone: "err", text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="mt-4 p-6">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-[17px] font-bold">유권자 명단</h2>
        <span className="text-[13px] text-ink-4">이름·학번은 서버의 명단 DB에만 저장되고, 블록체인에는 명단 해시만 올라가요</span>
      </div>

      {editable && !rows && (
        <div className="mt-4">
          <div
            className="flex flex-col items-center justify-center rounded-2xl border-2 border-dashed border-line bg-surface px-6 py-8 text-center"
            onDragOver={(ev) => ev.preventDefault()}
            onDrop={(ev) => {
              ev.preventDefault();
              onFile(ev.dataTransfer.files[0]);
            }}
          >
            <span className="text-3xl">📄</span>
            <p className="mt-3 text-[15px] font-semibold">CSV 파일을 끌어다 놓거나 선택하세요</p>
            <p className="mt-1 text-[13px] text-ink-3">열: 학번(external_ref), 이름(name), 이메일(email, 선택) · 엑셀에서 저장한 CSV도 돼요</p>
            <div className="mt-4 flex flex-wrap justify-center gap-2">
              <Button variant="secondary" onClick={() => fileRef.current?.click()}>
                파일 선택
              </Button>
              <Button variant="ghost" onClick={() => setPaste((p) => !p)}>
                직접 붙여넣기
              </Button>
              <Button variant="ghost" onClick={() => download("roster-sample.csv", SAMPLE)}>
                양식 받기
              </Button>
            </div>
            <input ref={fileRef} type="file" accept=".csv,text/csv,.txt" className="hidden" onChange={(ev) => onFile(ev.target.files?.[0])} />
          </div>
          {paste && (
            <div className="mt-3">
              <textarea
                value={text}
                onChange={(ev) => setText(ev.target.value)}
                rows={5}
                placeholder={"20260001,김하늘,sky@example.ac.kr\n20260002,이바다"}
                className="mono w-full rounded-xl border border-line p-3 text-[13px] outline-none focus:border-primary"
              />
              <Button className="mt-2" variant="secondary" disabled={!text.trim()} onClick={() => (setFileName("붙여넣은 명단"), setRows(toRows(text)))}>
                확인하기
              </Button>
            </div>
          )}
        </div>
      )}

      {editable && rows && (
        <div className="mt-4">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[14px]">
            <span className="font-semibold">{fileName}</span>
            <span className="text-ink-3">총 {rows.length}명</span>
            {errors.length > 0 ? <span className="font-semibold text-danger">오류 {errors.length}건</span> : <span className="font-semibold text-ok">오류 없음</span>}
          </div>
          <div className="mt-3 max-h-72 overflow-auto rounded-xl border border-line">
            <table className="w-full min-w-[420px] text-left text-[14px]">
              <thead className="sticky top-0 bg-surface text-[13px] text-ink-3">
                <tr>
                  <th className="px-4 py-2 font-semibold">행</th>
                  <th className="py-2 font-semibold">학번</th>
                  <th className="py-2 font-semibold">이름</th>
                  <th className="py-2 font-semibold">이메일</th>
                  <th className="px-4 py-2 font-semibold">확인</th>
                </tr>
              </thead>
              <tbody>
                {[...errors, ...rows.filter((r) => !r.error)].slice(0, 200).map((r) => (
                  <tr key={r.line} className={`border-t border-line ${r.error ? "bg-danger-soft/50" : ""}`}>
                    <td className="px-4 py-2 text-ink-4">{r.line}</td>
                    <td className="mono py-2">{r.external_ref}</td>
                    <td className="py-2">{r.name}</td>
                    <td className="py-2 text-ink-3">{r.email ?? "—"}</td>
                    <td className={`px-4 py-2 font-semibold ${r.error ? "text-danger" : "text-ok"}`}>{r.error ?? "✓"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt-4 flex flex-wrap justify-end gap-2">
            <Button variant="ghost" onClick={() => setRows(undefined)}>
              다시 고르기
            </Button>
            <Button disabled={busy || errors.length > 0 || !rows.length} onClick={save}>
              {errors.length ? "오류를 고친 뒤 등록할 수 있어요" : `명단 ${rows.length}명 등록`}
            </Button>
          </div>
        </div>
      )}

      {!rows && saved && saved.length > 0 && (
        <div className="mt-4">
          <p className="text-[14px] text-ink-2">
            등록된 명단 <b>{saved.length}명</b>
            {editable ? " · 확정 전이라 다시 올리면 전체가 바뀌어요" : " · 확정됨 (바꾸려면 기관 승인이 필요한 명단 정정)"}
          </p>
          {hash && <p className="mono mt-1 break-all text-[12px] text-ink-4">roster_hash {hash}{api.capabilities.mode === "mock" && " (mock 값)"}</p>}
          <details className="mt-3 rounded-xl border border-line">
            <summary className="cursor-pointer px-4 py-2.5 text-[14px] font-semibold text-ink-3">명단 보기</summary>
            <ul className="max-h-60 overflow-auto border-t border-line text-[14px]">
              {saved.map((v) => (
                <li key={v.voter_key} className="flex gap-4 border-b border-line px-4 py-2 last:border-0">
                  <span className="mono w-28 shrink-0">{v.external_ref}</span>
                  <span>{v.name}</span>
                </li>
              ))}
            </ul>
          </details>
        </div>
      )}

      {msg && <p className={`mt-4 rounded-xl p-3 text-[14px] ${msg.tone === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>{msg.text}</p>}
    </Card>
  );
}

// ───────────── link delivery (ROSTER_FROZEN / OPEN) ─────────────

type LinkRow = { external_ref: string; name: string };

export function LinkDeliveryCard({ e, onChange }: { e: Election; onChange: () => void }) {
  const [csv, setCsv] = useState("");
  const [links, setLinks] = useState<LinkRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [copied, setCopied] = useState("");

  const template = `[${e.title}] 투표 안내\n1회용 투표 링크입니다. 이 링크를 가진 사람은 누구나 한 번 투표할 수 있으니 다른 사람에게 전달하지 마세요.\n투표 기간: ${new Date(e.window_start).toLocaleString("ko-KR")} ~ ${new Date(e.window_end).toLocaleString("ko-KR")}\n링크: {링크}`;

  async function issue() {
    setBusy(true);
    setNote("");
    try {
      const text = await api.issueTokens(e.election_id);
      if (!text) {
        setNote("새로 발급할 유권자가 없어요. 링크 원문은 처음 발급할 때만 받을 수 있어요.");
        return;
      }
      setCsv(text);
      setLinks(
        parseCsv(text)
          .slice(1)
          .map(([external_ref, name]) => ({ external_ref, name })),
      );
      download(`${e.election_id}-links.csv`, text);
      onChange();
    } finally {
      setBusy(false);
    }
  }

  function copy(key: string, value: string) {
    navigator.clipboard?.writeText(value);
    setCopied(key);
    setTimeout(() => setCopied(""), 1500);
  }

  const steps = [
    ["링크 발급", "유권자마다 1회용 링크를 만들어 CSV로 받아요. 서버에는 링크의 해시만 남아요."],
    ["각자에게 전달", "CSV의 링크를 문자·메일·메신저로 해당 유권자에게만 보내요. 아래 안내 문구를 쓰면 돼요."],
    ["투표 시작", "시작 시각이 되면 링크를 연 유권자가 휴대폰에서 바로 투표해요."],
  ];

  return (
    <Card className="mt-4 p-6">
      <h2 className="text-[17px] font-bold">투표 링크 전달</h2>
      <ol className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
        {steps.map(([t, d], i) => {
          const done = i === 0 ? e.counters.tokens_active + e.counters.tokens_used > 0 : i === 1 ? false : e.status === "OPEN";
          return (
            <li key={t} className="rounded-xl bg-surface p-4">
              <span className={`flex h-7 w-7 items-center justify-center rounded-full text-[13px] font-bold ${done ? "bg-primary text-white" : "bg-white text-ink-3"}`}>
                {done ? "✓" : i + 1}
              </span>
              <p className="mt-2 text-[15px] font-semibold">{t}</p>
              <p className="mt-1 text-[13px] leading-relaxed text-ink-3">{d}</p>
            </li>
          );
        })}
      </ol>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Button disabled={busy} onClick={issue}>
          {e.counters.tokens_active + e.counters.tokens_used > 0 ? "추가 유권자 링크 발급" : `링크 발급 · CSV 받기 (${e.voter_count}명)`}
        </Button>
        {csv && (
          <Button variant="secondary" onClick={() => download(`${e.election_id}-links.csv`, csv)}>
            CSV 다시 받기
          </Button>
        )}
        <Button variant="ghost" disabled title="인증·전달 방법은 다음 단계에서 검토해요">
          메일로 보내기 (준비 중)
        </Button>
      </div>
      <p className="mt-3 rounded-xl bg-warn-soft p-3 text-[13px] text-warn">
        이 CSV를 가진 사람은 누구든 해당 유권자 대신 투표할 수 있어요(시스템이 막지 못해요). 링크 원문은 이 CSV에만 있으니 안전한 곳에 보관하고, 전달이 끝나면 지우세요. 링크를 복사해서 보내면 클립보드 기록에도 남을 수 있어요.
      </p>
      {note && <p className="mt-3 text-[14px] text-ink-3">{note}</p>}

      <div className="mt-5">
        <div className="flex items-center justify-between">
          <span className="text-[14px] font-semibold text-ink-3">안내 문구</span>
          <Button variant="ghost" className="h-8 px-3 text-[13px]" onClick={() => copy("template", template)}>
            {copied === "template" ? "복사됨" : "복사"}
          </Button>
        </div>
        <pre className="mt-2 whitespace-pre-wrap rounded-xl bg-surface p-4 text-[13px] leading-relaxed text-ink-2">{template}</pre>
      </div>

      {links.length > 0 && (
        <div className="mt-5">
          <span className="text-[14px] font-semibold text-ink-3">발급 완료 {links.length}명 (링크 원문은 CSV에서만 확인)</span>
          <ul className="mt-2 max-h-64 overflow-auto rounded-xl border border-line text-[14px]">
            {links.map((l) => (
              <li key={l.external_ref} className="flex items-center gap-3 border-b border-line px-4 py-2 last:border-0">
                <span className="mono w-24 shrink-0 text-ink-3">{l.external_ref}</span>
                <span className="min-w-0 flex-1 truncate">{l.name}</span>
                <span className="shrink-0 text-[13px] text-ok">발급됨</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}
