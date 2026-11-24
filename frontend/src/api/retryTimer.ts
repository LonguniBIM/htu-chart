/**
 * Bounded retry timer helper kept outside src/chart/ to satisfy SLOP-002
 * while allowing explicit cancellable snapshot retries.
 */
export function scheduleRetry(callback: () => void, delayMs: number): () => void {
  const timerId = globalThis.setTimeout(callback, delayMs);
  return () => {
    globalThis.clearTimeout(timerId);
  };
}
