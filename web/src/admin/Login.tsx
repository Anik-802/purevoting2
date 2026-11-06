import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Button } from "../ui";

export default function Login() {
  const nav = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.login(username, password);
      nav("/admin");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const input = "h-12 w-full rounded-xl border border-line px-4 text-[15px] outline-none focus:border-primary";

  return (
    <div className="flex min-h-screen items-center justify-center bg-surface px-6">
      <form onSubmit={submit} className="w-full max-w-sm rounded-3xl bg-white p-8 shadow-[0_8px_32px_rgba(0,27,55,0.08)]">
        <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary text-lg font-black text-white">P</span>
        <h1 className="mt-5 text-[22px] font-bold">선거 관리자 로그인</h1>
        <p className="mt-1 text-[14px] text-ink-3">선거관리 담당자 계정으로 로그인하세요.</p>
        <div className="mt-6 space-y-3">
          <input className={input} placeholder="아이디" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
          <input
            className={input}
            placeholder="비밀번호"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </div>
        {error && <p className="mt-3 text-[14px] text-danger">{error}</p>}
        <Button type="submit" size="lg" className="mt-6 w-full" disabled={busy}>
          로그인
        </Button>
      </form>
    </div>
  );
}
