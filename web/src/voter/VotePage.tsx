import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { ApiError, type BallotReceipt, type Candidate, type PublicElection } from "../api/types";
import { BottomSheet, Button, Spinner, fmtDate } from "../ui";

type Step = "loading" | "waiting" | "intro" | "select" | "sending" | "done" | "error";

const credKey = async (token: string) => `pv_cred_${await api.sha256Hex(token)}`;

const randomCredential = () =>
  btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(16))))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");

export default function VotePage() {
  const { electionId = "" } = useParams();
  // read once: the token is removed from the address bar after the vote
  const [token] = useState(() => new URLSearchParams(location.hash.slice(1)).get("t") ?? "");
  const [step, setStep] = useState<Step>("loading");
  const [election, setElection] = useState<PublicElection>();
  const [choice, setChoice] = useState<Candidate>();
  const [confirm, setConfirm] = useState(false);
  const [receipt, setReceipt] = useState<BallotReceipt>();
  const [error, setError] = useState("");
  // an earlier attempt from this browser whose answer was lost: its sealed ballot is resent as is
  const [pending, setPending] = useState(false);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const load = () =>
      api.getPublicElection(electionId)
        .then(async (e) => {
          setElection(e);
          if (!token) throw new ApiError("TOKEN_INVALID", "투표 링크가 올바르지 않습니다. 받은 링크를 그대로 열어 주세요.");
          // an unconfirmed ballot from this browser can be resent even after closing (core-api submitBallot: identical
          // retries return the original receipt)
          const cred = localStorage.getItem(await credKey(token));
          if (cred && (await api.hasPendingBallot(cred))) {
            setPending(true);
            setStep("intro");
            return;
          }
          // opened before the start: wait and check again (Core opens the election at window_start)
          if ((e.status === "DRAFT" || e.status === "ROSTER_FROZEN") && Date.now() < new Date(e.window_end).getTime()) {
            setStep("waiting");
            timer = setTimeout(load, Math.min(Math.max(new Date(e.window_start).getTime() - Date.now() + 2000, 3000), 15000));
            return;
          }
          if (e.status !== "OPEN") throw new ApiError("ELECTION_NOT_OPEN", "지금은 투표 기간이 아닙니다.");
          setStep("intro");
        })
        .catch((e: Error) => {
          setError(e.message);
          setStep("error");
        });
    load();
    return () => clearTimeout(timer);
  }, [electionId, token]);

  // poll the receipt until the ballot is anchored on the ledger (5 s, backing off to 15 s: the public entry caps the
  // whole site's receipt requests, D-29)
  useEffect(() => {
    if (!receipt || receipt.status !== "RECEIVED") return;
    let delay = 5000;
    let t: ReturnType<typeof setTimeout>;
    const poll = () =>
      api.getReceipt(receipt.receipt_hash)
        .then((r) => (r.status === "RECEIVED" ? next() : setReceipt(r)))
        .catch(next);
    const next = () => {
      t = setTimeout(poll, delay);
      delay = Math.min(delay * 1.5, 15000);
    };
    next();
    return () => clearTimeout(t);
  }, [receipt]);

  async function cast() {
    if ((!choice && !pending) || !election) return;
    setConfirm(false);
    setStep("sending");
    try {
      // the credential is stored before redeem so a lost response can be retried (core-api: redeemToken); keyed by
      // H(token) so two voters sharing a browser never mix, and deleted once the ballot is confirmed (threat-model S9)
      const key = await credKey(token);
      const stored = localStorage.getItem(key);
      if (pending && !stored) throw new ApiError("ALREADY_DONE", "이 표는 다른 창에서 이미 접수 확인을 받았어요. 영수증 확인 화면에서 확인해 주세요.");
      const credential = stored ?? randomCredential();
      localStorage.setItem(key, credential);
      try {
        // a pending ballot already registered its credential; after closing, redeem would only be refused
        if (!pending) await api.redeemToken(election.election_id, token, await api.sha256Hex(credential));
      } catch (e) {
        if (!(e instanceof ApiError && e.code === "TOKEN_ALREADY_USED")) throw e;
      }
      try {
        setReceipt(await api.submitBallot(election.election_id, credential, choice?.candidate_id ?? ""));
      } catch (e) {
        // the credential was never registered (the link was used elsewhere): nothing here can ever be sent
        if (e instanceof ApiError && e.code === "CREDENTIAL_UNKNOWN") {
          await api.discardBallot(credential);
          localStorage.removeItem(key);
        }
        throw e;
      }
      localStorage.removeItem(key);
      history.replaceState(null, "", location.pathname); // drop the token from the address bar
      setStep("done");
    } catch (e) {
      setError((e as Error).message);
      setStep("error");
    }
  }

  return (
    <div className="min-h-screen bg-white">
      <div className="mx-auto flex min-h-screen max-w-md flex-col px-6">
        <header className="flex h-14 items-center">
          <span className="text-[17px] font-bold text-primary">PureVoting</span>
        </header>

        {step === "loading" && (
          <div className="flex flex-1 items-center justify-center text-primary">
            <Spinner className="h-8 w-8" />
          </div>
        )}

        {step === "waiting" && election && (
          <Center
            icon="⏰"
            title="아직 투표 시작 전이에요"
            body={`${election.title}\n${fmtDate(election.window_start)}에 시작해요. 이 화면을 열어 두면 시작될 때 자동으로 넘어가요.`}
          />
        )}

        {step === "error" && (
          <Center icon="⚠️" title="투표를 진행할 수 없어요" body={error}>
            <Link to="/receipt" className="text-[15px] font-semibold text-primary">
              이미 투표했다면 영수증 확인하기
            </Link>
          </Center>
        )}

        {step === "intro" && election && (
          <>
            <div className="flex-1 pt-8">
              <p className="text-[15px] font-semibold text-primary">투표 안내</p>
              <h1 className="mt-2 text-[26px] font-bold leading-snug">{election.title}</h1>
              <dl className="mt-8 space-y-4 rounded-2xl bg-surface p-5 text-[15px]">
                <Row label="투표 기간" value={`${fmtDate(election.window_start)} ~ ${fmtDate(election.window_end)}`} />
                <Row label="후보" value={`${election.candidates.length}명`} />
              </dl>
              <ul className="mt-8 space-y-3 text-[15px] leading-relaxed text-ink-3">
                <li>• 한 번만 투표할 수 있고, 투표 후에는 바꿀 수 없어요.</li>
                <li>• 투표 내용은 이 기기에서 암호화된 뒤 전송되고, 서버에는 신원 정보와 분리해서 저장돼요.</li>
                <li>• 투표가 끝나면 영수증으로 내 표가 기록됐는지 확인할 수 있어요.</li>
              </ul>
            </div>
            <BottomCta>
              {pending ? (
                <>
                  <p className="mb-3 rounded-xl bg-warn-soft p-3 text-[14px] text-warn">
                    이 기기에서 보낸 표의 접수 확인을 받지 못했어요. 처음 고른 그대로 다시 보내서 확인할게요.
                  </p>
                  <Button size="lg" className="w-full" onClick={cast}>
                    이전 표 다시 보내기
                  </Button>
                </>
              ) : (
                <Button
                  size="lg"
                  className="w-full"
                  onClick={() => {
                    api.prepareBallot();
                    setStep("select");
                  }}
                >
                  투표 시작하기
                </Button>
              )}
            </BottomCta>
          </>
        )}

        {step === "select" && election && (
          <>
            <div className="flex-1 pt-8">
              <h1 className="text-[24px] font-bold leading-snug">
                투표할 후보를
                <br />
                선택해 주세요
              </h1>
              <div className="mt-8 space-y-3" role="radiogroup">
                {election.candidates.map((c) => {
                  const on = choice?.candidate_id === c.candidate_id;
                  return (
                    <button
                      key={c.candidate_id}
                      role="radio"
                      aria-checked={on}
                      onClick={() => setChoice(c)}
                      className={`flex w-full items-center gap-4 rounded-2xl border-2 p-5 text-left transition-colors ${
                        on ? "border-primary bg-primary-soft" : "border-line hover:bg-surface"
                      }`}
                    >
                      <span
                        className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full border-2 ${
                          on ? "border-primary bg-primary" : "border-ink-4/50"
                        }`}
                      >
                        {on && <span className="h-2.5 w-2.5 rounded-full bg-white" />}
                      </span>
                      <span>
                        <span className="block text-[17px] font-semibold">{c.name}</span>
                        {c.description && <span className="mt-1 block text-[14px] text-ink-3">{c.description}</span>}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
            <BottomCta>
              <Button size="lg" className="w-full" disabled={!choice} onClick={() => setConfirm(true)}>
                다음
              </Button>
            </BottomCta>
          </>
        )}

        {step === "sending" && (
          <Center icon={<Spinner className="h-10 w-10 text-primary" />} title="투표를 안전하게 보내고 있어요" body="투표지를 암호화해서 전송하는 중이에요. 화면을 닫지 마세요." />
        )}

        {step === "done" && receipt && (
          <>
            <div className="flex-1 pt-16 text-center">
              <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-primary text-3xl text-white">✓</div>
              <h1 className="mt-6 text-[24px] font-bold">투표가 완료됐어요</h1>
              <p className="mt-2 text-[15px] text-ink-3">아래 영수증 번호로 내 표가 기록됐는지 확인할 수 있어요.</p>
              <div className="mt-8 rounded-2xl bg-surface p-5 text-left">
                <div className="flex items-center justify-between">
                  <span className="text-[14px] font-semibold text-ink-3">영수증 번호</span>
                  <ReceiptState status={receipt.status} />
                </div>
                <p className="mono mt-2 break-all text-[13px] leading-relaxed text-ink-2">{receipt.receipt_hash}</p>
              </div>
            </div>
            <BottomCta>
              <Button size="lg" variant="secondary" className="w-full" onClick={() => navigator.clipboard?.writeText(receipt.receipt_hash)}>
                영수증 번호 복사
              </Button>
            </BottomCta>
          </>
        )}
      </div>

      <BottomSheet open={confirm} onClose={() => setConfirm(false)}>
        <h2 className="text-[20px] font-bold leading-snug">
          {choice?.name}
          <br />
          후보에게 투표할까요?
        </h2>
        <p className="mt-2 text-[15px] text-ink-3">투표한 뒤에는 바꿀 수 없어요.</p>
        <div className="mt-6 flex gap-2">
          <Button size="lg" variant="ghost" className="flex-1" onClick={() => setConfirm(false)}>
            다시 고를게요
          </Button>
          <Button size="lg" className="flex-1" onClick={cast}>
            투표하기
          </Button>
        </div>
      </BottomSheet>
    </div>
  );
}

export function ReceiptState({ status }: { status: BallotReceipt["status"] }) {
  if (status === "ANCHORED") return <span className="text-[14px] font-semibold text-ok">● 블록체인 기록 완료</span>;
  if (status === "LEDGER_REJECTED") return <span className="text-[14px] font-semibold text-danger">● 기록 거부됨</span>;
  return (
    <span className="inline-flex items-center gap-1.5 text-[14px] font-semibold text-warn">
      <Spinner className="h-3.5 w-3.5" /> 기록 대기 중
    </span>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4">
      <dt className="shrink-0 text-ink-3">{label}</dt>
      <dd className="text-right font-semibold">{value}</dd>
    </div>
  );
}

function BottomCta({ children }: { children: React.ReactNode }) {
  return <div className="sticky bottom-0 bg-white pb-6 pt-3">{children}</div>;
}

function Center({ icon, title, body, children }: { icon: React.ReactNode; title: string; body: string; children?: React.ReactNode }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center pb-24 text-center">
      <div className="text-4xl">{icon}</div>
      <h1 className="mt-5 text-[22px] font-bold">{title}</h1>
      <p className="mt-2 whitespace-pre-line text-[15px] leading-relaxed text-ink-3">{body}</p>
      {children && <div className="mt-6">{children}</div>}
    </div>
  );
}
