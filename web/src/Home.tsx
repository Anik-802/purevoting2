import { Link } from "react-router-dom";
import { api } from "./api";

// Entry page: links to both webs (the demo voting link only with the mock API).
export default function Home() {
  const card = "block rounded-2xl border border-line p-6 transition-colors hover:border-primary hover:bg-primary-soft/40";
  return (
    <div className="mx-auto max-w-md px-6 py-16">
      <span className="text-[17px] font-bold text-primary">PureVoting</span>
      <h1 className="mt-3 text-[26px] font-bold leading-snug">
        투명하고 안전한
        <br />
        온라인 투표
      </h1>
      <div className="mt-10 space-y-3">
        {api.capabilities.mode === "mock" ? (
          <Link className={card} to="/vote/el-2026-student#t=demo-token">
            <div className="text-[17px] font-bold">투표하기</div>
            <div className="mt-1 text-[14px] text-ink-3">받은 투표 링크로 투표하는 화면 (데모 링크)</div>
          </Link>
        ) : (
          <div className={card}>
            <div className="text-[17px] font-bold">투표하기</div>
            <div className="mt-1 text-[14px] text-ink-3">문자·메일로 받은 본인 투표 링크를 열어 주세요</div>
          </div>
        )}
        <Link className={card} to="/receipt">
          <div className="text-[17px] font-bold">영수증 확인</div>
          <div className="mt-1 text-[14px] text-ink-3">내 표가 블록체인에 기록됐는지 확인</div>
        </Link>
        <Link className={card} to="/admin">
          <div className="text-[17px] font-bold">선거 관리자</div>
          <div className="mt-1 text-[14px] text-ink-3">선거 생성, 명단, 개표, 검사 결과</div>
        </Link>
      </div>
    </div>
  );
}
