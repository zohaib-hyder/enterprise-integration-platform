export type Mapping = { id: string; connection_id: string; source_kind: string; entity_type: string; name: string; active_version: number };
export type Step = { operation: "trim" | "email" | "enum" | "datetime" | "number" | "string"; values?: Record<string, string> | null; numeric_type?: "integer" | "decimal"; max_length?: number };
export type FieldRule = { target: string; source: string | null; required: boolean; null_handling: "reject" | "omit" | "default" | "preserve"; default?: string | number | boolean | string[] | null; steps: Step[] };
export type Definition = { schema_version: 1; fields: FieldRule[] };
export type MappingVersion = { id: string; mapping_id: string; version: number; definition: Definition; definition_digest: string; created_at: string };
export type Template = { source_type: string; source_kind: string; entity_type: string; source_schema: object; target_schema?: object; source_paths: string[]; target_fields: string[]; definition: Definition };
export type CanonicalRecord = { canonical_id: string; entity_type: string; source_connection_id: string; source_external_id: string; source_updated_at: string | null; normalized_at: string; normalized_data: Record<string, unknown>; field_provenance: object; revision: number };
export type Attempt = { id: string; mapping_version_id: string; canonical_id: string | null; event_id: string | null; external_id: string; outcome: string; issues: { category: string; field: string | null }[]; result_snapshot: object | null };
export type Invalid = { id: string; status: string; resolution_attempt_id: string | null; attempt: Attempt };
export type ExternalIdentity = { id: string; connection_id: string; source_kind: string; external_id: string; canonical_id: string; provenance: string };
export type Authority = { id: string; entity_type: string; field: string; owner: string };
export function exampleSource(kind: string): object {
  return { data: kind === "contact" ? { name: "Synthetic Contact", email: "contact@example.test", company: "Fictional Harbor" } : kind === "candidate" ? { name: "Synthetic Candidate", email: "candidate@example.test", stage: "NEW", skills: ["Python"] } : { title: "Synthetic Job", department: "Technology", location: "Toronto", status: "OPEN" } };
}
