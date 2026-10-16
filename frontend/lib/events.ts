export type Source = "synthetic_crm" | "synthetic_ats";
export type WebhookEndpoint = {
  id: string; connection_id: string; source: Source; status: "ACTIVE" | "DISABLED";
  public_id: string; signature_version: string; timestamp_tolerance: number;
  current_secret_version: number; previous_secret_valid_until: string | null;
};
export type InboxEvent = {
  id: string; endpoint_id: string; connection_id: string; source: Source;
  provider_event_id: string; event_type: string; schema_version: number;
  ingestion_status: "ACCEPTED"; processing_status: "PENDING";
  received_at: string; occurred_at: string; duplicate_count: number;
  correlation_id: string; payload: Record<string, unknown> | null;
};
export type Receipt = {
  id: string; endpoint_id: string; event_id: string | null; outcome: string;
  error_category: string | null; duration_ms: number; correlation_id: string; received_at: string;
};
export type Disclosure = { endpoint: WebhookEndpoint; signing_secret: string; secret_version: number };
export type EndpointStatus = {
  status: string; connection_status: string; secret_available: boolean;
  receipt_counts: Record<string, number>; error_counts: Record<string, number>;
  last_received_at: string | null;
};
