import { Link, NavLink, Navigate, Outlet, useNavigate } from "react-router-dom";
import { api } from "../api";

const { currentStaff, logout } = api;

export default function AdminLayout() {
  const nav = useNavigate();
  const staff = currentStaff();
  if (!staff) return <Navigate to="/admin/login" replace />;

  const tab = ({ isActive }: { isActive: boolean }) =>
    `h-full inline-flex items-center border-b-[3px] px-1 text-[16px] font-bold ${isActive ? "border-primary text-ink" : "border-transparent text-ink-3 hover:text-ink"}`;

  return (
    <div className="min-h-screen bg-surface">
      {/* notice bar (NEC style) */}
      <div className="bg-navy text-[13px] text-white/85">
        <div className="mx-auto flex h-9 max-w-6xl items-center gap-2 px-4 sm:px-6">
          <span className="shrink-0 rounded bg-white/15 px-1.5 py-0.5 text-[12px] font-semibold">알림</span>
          <span className="truncate">투표 데이터는 시간 구간마다 블록체인에 기록되고, 규칙 엔진이 체크포인트마다 검사합니다.</span>
        </div>
      </div>
      <header className="border-b border-line bg-white">
        <div className="mx-auto flex h-16 max-w-6xl items-center gap-4 px-4 sm:gap-10 sm:px-6">
          <Link to="/admin" className="flex items-center gap-2">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-[15px] font-black text-white">P</span>
            <span className="hidden leading-tight sm:block">
              <span className="block text-[17px] font-bold">PureVoting</span>
              <span className="block text-[11px] font-medium tracking-wide text-ink-4">ELECTION ADMIN</span>
            </span>
          </Link>
          <nav className="flex h-full gap-5 sm:gap-8">
            <NavLink to="/admin" end className={tab}>
              선거 관리
            </NavLink>
            <NavLink to="/admin/new" className={tab}>
              선거 만들기
            </NavLink>
          </nav>
          <div className="ml-auto flex items-center gap-3 text-[14px] text-ink-3">
            <span className="hidden sm:inline">{staff} 님</span>
            <span className="hidden text-line sm:inline">|</span>
            <button
              className="hover:text-ink"
              onClick={() => {
                logout();
                nav("/admin/login");
              }}
            >
              로그아웃
            </button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6 sm:px-6 sm:py-8">
        <Outlet />
      </main>
    </div>
  );
}
