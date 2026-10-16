export type ConnectorType = "synthetic_crm" | "synthetic_ats" | "synthetic_slack" | "synthetic_email" | "synthetic_calendar" | "generic_rest";
export type Config = { schema_version?: number; workspace_label?: string; allowed_channels?: string[]; allowed_recipients?: string[]; allowed_calendars?: string[]; mode?: "disabled" };
export type Capability = { connector_type: ConnectorType; name: string; synthetic: boolean; enabled: boolean; limitation: string | null; config_schema_version: number; config_schema: object; operations: OperationSpec[] };
export type OperationSpec = { name: string; schema_version: number; mutating: boolean; payload_schema: object };
export type Connection = { id: string; connector_type: ConnectorType; name: string; status: "ENABLED" | "DISABLED"; public_config: Config; health_status: string; last_checked_at: string | null; synthetic: boolean };
export type SyntheticRecord = { id: string; external_id?: string; kind?: string; data?: Record<string, string | string[]>; channel?: string; text?: string; recipient?: string; subject?: string; body?: string; calendar?: string; title?: string; starts_at?: string; ends_at?: string; status?: string; synthetic: boolean };
export type OperationResult = { schema_version: number; operation: string; synthetic: true; data: Record<string, unknown> };
export const defaultConfig = (type: ConnectorType): Config => type === "synthetic_slack" ? { schema_version: 1, allowed_channels: ["recruiting"] } : type === "synthetic_email" ? { schema_version: 1, allowed_recipients: ["recruiter@example.test"] } : type === "synthetic_calendar" ? { schema_version: 1, allowed_calendars: ["interviews"] } : type === "generic_rest" ? { schema_version: 1, mode: "disabled" } : { schema_version: 1, workspace_label: "Synthetic business workspace" };
export function examplePayload(operation: string, type: ConnectorType): object {
  if (operation.endsWith(".list")) return { offset: 0, limit: 25 };
  if (operation.endsWith(".get") || operation === "event.cancel") return { id: "PASTE_A_SYNTHETIC_RECORD_UUID" };
  if (operation === "record.search") return { kind: type === "synthetic_ats" ? "candidate" : "contact", external_id: type === "synthetic_ats" ? "demo-candidate-1" : "demo-contact-1" };
  if (operation === "contact.upsert") return { external_id: "demo-contact-1", data: { name: "Synthetic Contact", email: "contact@example.test", company: "Fictional Harbor Company" } };
  if (operation === "candidate.upsert") return { external_id: "demo-candidate-1", data: { name: "Synthetic Candidate", email: "candidate@example.test", stage: "NEW", skills: ["Python"] } };
  if (operation === "requisition.upsert") return { external_id: "demo-job-1", data: { title: "Synthetic Integration Specialist", department: "Technology", location: "Toronto", status: "OPEN" } };
  if (operation === "message.send") return type === "synthetic_slack" ? { channel: "recruiting", text: "Synthetic local notification", idempotency_key: "demo-message-send-v1" } : { recipient: "recruiter@example.test", subject: "Synthetic coordination", body: "Local simulation only", idempotency_key: "demo-email-send-v1" };
  const fields = { calendar: "interviews", title: "Synthetic interview", starts_at: "2030-01-16T15:00:00Z", ends_at: "2030-01-16T16:00:00Z" };
  return operation === "event.create" ? { ...fields, idempotency_key: "demo-event-create-v1" } : { ...fields, id: "PASTE_A_SYNTHETIC_EVENT_UUID" };
}
