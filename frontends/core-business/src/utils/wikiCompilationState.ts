import type { WikiCompilationState } from '@/api/domainKnowledge';

export function wikiCompilationStatus(state: WikiCompilationState | null, error = false): string {
  if (error) return 'loadFailed';
  if (!state) return 'loading';
  if (state.paused) return 'paused';
  if (state.tasks.some(t => t.status === 'compiling')) return 'active';
  if (state.pending_source_count) return 'parsing';
  if (state.tasks.length) return 'queued';
  if (state.waiting_source_count) return 'waiting';
  if (state.failed_source_count || state.last_task_status === 'failed' || state.last_task_status === 'cancelled') return 'failed';
  if (state.parsed_source_count > state.published_source_count) return 'waiting';
  return state.parsed_source_count ? 'complete' : 'empty';
}
