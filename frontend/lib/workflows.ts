export type Step = { id: string; type: string; config: Record<string, unknown>; retry?: { max_attempts: number; base_delay_seconds: number; max_delay_seconds: number; jitter_fraction: number } };
export type Plan = { schema_version: 1; trigger: { connection_id: string; source: string; event_types: string[]; event_id_prefix?: string | null }; steps: Step[] };
export type Workflow = { id: string; name: string; description: string; status: string; active_version: number; trigger_configuration: Plan['trigger'] };
export type Version = { id: string; version: number; definition: Plan; created_at: string };
export type Checkpoint = { id: string; step_id: string; position: number; step_type: string; state: string; attempt_count: number; error_category: string | null; checkpoint_saved: boolean };
export type Execution = { id: string; workflow_id: string; event_id: string; root_id: string; replay_of_id: string | null; state: string; cursor: number; cancel_requested: boolean; error_category: string | null; created_at: string; next_run_at: string; steps?: Checkpoint[] };
export type History = { id: string; step_id: string | null; event_type: string; category: string | null; created_at: string };
export type Retry = { id: string; attempt: number; available_at: string; state: string; category: string };
export type DeadLetter = { id: string; execution_id: string; event_id: string; step_id: string; category: string; attempt_count: number };
export type WorkerStatus = { active_workers: { id: string; heartbeat_at: string }[]; backlog: Record<string, number>; checked_at: string };
export function workflowTemplate(source: string, connection: string, mappingVersion: string, retryDemo: boolean): Plan {
  return { schema_version: 1, trigger: { connection_id: connection, source, event_types: ['job.updated'] }, steps: [
    { id: 'normalize', type: 'NORMALIZE_EVENT', config: { mapping_version_id: mappingVersion } },
    { id: 'condition', type: 'CONDITION', config: { reference: { source: 'step', step_id: 'normalize', path: 'normalized_data.title' }, operator: 'EXISTS' } },
    ...(retryDemo ? [{ id: 'delay', type: 'DELAY', config: { seconds: 2 } }] : []),
    { id: 'observe', type: 'MOCK_CONNECTOR_ACTION', config: { connection_id: connection, operation: 'record.search', payload: { kind: 'requisition' }, bindings: { external_id: { source: 'event', path: 'external_id' } }, fault: { mode: retryDemo ? 'TRANSIENT' : 'NONE', fail_first_attempts: 1 } } },
    { id: 'result', type: 'RECORD_RESULT', config: { summary: 'Synthetic job observed', outcome: 'OBSERVED', canonical_id: { source: 'step', step_id: 'normalize', path: 'canonical_id' } } },
  ] };
}
