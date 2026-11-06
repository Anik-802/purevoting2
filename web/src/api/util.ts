import type { Election } from "./types";

/** D-10 sizing rule: expected voters per time bucket = voter_count × bucket ÷ window length (freeze needs ≥ 10) */
export const votersPerBucket = (e: Pick<Election, "window_start" | "window_end" | "time_bucket_seconds">, voterCount: number) =>
  (voterCount * e.time_bucket_seconds) / ((new Date(e.window_end).getTime() - new Date(e.window_start).getTime()) / 1000);
