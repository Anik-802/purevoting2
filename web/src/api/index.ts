// One API surface for the screens. VITE_API_MODE=mock (default for `npm run dev`) uses the in-memory stand-in,
// anything else (default for builds, e.g. the compose `web` service) talks to Core through /v1/.
import * as mock from "./mock";
import * as real from "./real";

const mode = import.meta.env.VITE_API_MODE ?? (import.meta.env.DEV ? "mock" : "real");
export const api: typeof mock = mode === "mock" ? mock : real;
