import { useCallback, useEffect, useRef, useState } from 'react';
import { getWikiCompilationState, setWikiCompilationPaused } from '@/api/domainKnowledge';
import type { WikiCompilationState } from '@/api/domainKnowledge';
import { wikiCompilationStatus } from '@/utils/wikiCompilationState';

export function useWikiCompilation(kbId: string, enabled: boolean) {
  const [state, setState] = useState<WikiCompilationState | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const sequence = useRef(0);
  const mutating = useRef(false);
  const currentKb = useRef(kbId);
  currentKb.current = kbId;
  const refresh = useCallback(async () => {
    if (mutating.current) return;
    const request = ++sequence.current;
    try {
      const next = await getWikiCompilationState(kbId);
      if (currentKb.current === kbId && request === sequence.current) {
        setState(next); setError(false);
      }
    } catch {
      if (currentKb.current === kbId && request === sequence.current) setError(true);
    }
  }, [kbId]);
  useEffect(() => {
    setState(null); setError(false); setLoading(false);
    if (!enabled || !kbId) return;
    let alive = true;
    let timer: number;
    const poll = async () => {
      await refresh();
      if (alive) timer = window.setTimeout(poll, 5000);
    };
    void poll();
    return () => { alive = false; ++sequence.current; window.clearTimeout(timer); };
  }, [kbId, enabled, refresh]);
  const status = wikiCompilationStatus(state, error);
  const canToggle = !!state && !error && !loading && (state.paused || ['active', 'queued', 'waiting', 'parsing'].includes(status));
  const toggle = useCallback(async () => {
    if (!state || !canToggle || mutating.current) return;
    mutating.current = true;
    const request = ++sequence.current;
    setLoading(true);
    try {
      const next = await setWikiCompilationPaused(kbId, !state.paused);
      if (currentKb.current === kbId && request === sequence.current) {
        setState(next); setError(false);
      }
    } catch {
      if (currentKb.current === kbId && request === sequence.current) setError(true);
    } finally {
      mutating.current = false;
      if (currentKb.current === kbId) setLoading(false);
    }
  }, [kbId, state, canToggle]);
  return { state, loading, error, status, canToggle, refresh, toggle };
}
