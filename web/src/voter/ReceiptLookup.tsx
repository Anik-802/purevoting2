import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { BallotReceipt } from "../api/types";
import { Button } from "../ui";
import { ReceiptState } from "./VotePage";

export default function ReceiptLookup() {
  const [hash, setHash] = useState("");
  const [receipt, setReceipt] = useState<BallotReceipt>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function check() {
    setBusy(true);
    setError("");
    setReceipt(undefined);
    try {
      setReceipt(await api.getReceipt(hash));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto flex min-h-screen max-w-md flex-col px-6">
      <header className="flex h-14 items-center">
        <Link to="/" className="text-[17px] font-bold text-primary">PureVoting</Link>
      </header>
      <div className="flex-1 pt-8">
        <h1 className="text-[24px] font-bold leading-snug">
          내 투표가 기록됐는지
          <br />
          확인해 보세요
        </h1>
        <label className="mt-8 block text-[14px] font-semibold text-ink-3" htmlFor="hash">
          영수증 번호
        </label>
        <textarea
          id="hash"
          rows={3}
          value={hash}
          onChange={(e) => setHash(e.target.value)}
          placeholder="투표 완료 화면의 영수증 번호를 붙여 넣으세요"
          className="mono mt-2 w-full resize-none rounded-2xl border-2 border-line p-4 text-[14px] outline-none focus:border-primary"
        />
        {error && <p className="mt-3 text-[14px] text-danger">{error}</p>}
        {receipt && (
          <div className="mt-6 space-y-3 rounded-2xl bg-surface p-5 text-[14px]">
            <ReceiptState status={receipt.status} />
            {receipt.status === "ANCHORED" && (
              <>
                <p className="text-ink-3">내 표가 포함된 묶음이 블록체인에 기록됐어요. 이후 누구도 몰래 바꿀 수 없어요.</p>
                <p className="mono break-all text-[12px] text-ink-3">tx {receipt.tx_hash}</p>
              </>
            )}
            {receipt.status === "RECEIVED" && <p className="text-ink-3">투표는 접수됐고, 다음 기록 주기에 블록체인에 올라가요.</p>}
          </div>
        )}
      </div>
      <div className="sticky bottom-0 bg-white pb-6 pt-3">
        <Button size="lg" className="w-full" disabled={hash.trim().length < 64 || busy} onClick={check}>
          확인하기
        </Button>
      </div>
    </div>
  );
}
