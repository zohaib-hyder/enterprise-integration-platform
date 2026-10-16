export type SyncDefinition = {entity_type:'job_requisition';mapping_version_id:string;trigger_event_types:('job.created'|'job.updated')[];destination_fields:Record<string,string>;field_authority_policy:'TENANT_FIELD_AUTHORITY';create_enabled:boolean;update_enabled:boolean;conflict_policy:'HOLD';match_rule:'NONE'|'EXACT_TITLE_DEPARTMENT_LOCATION'};
export type SyncConfiguration = {id:string;name:string;source_connection_id:string;destination_connection_id:string;active_version_id:string;enabled:boolean;revision:number;workflow_id:string|null};
export type SyncVersion = {id:string;version:number;definition:SyncDefinition};
export type SyncPlan = {id:string;decision:string;digest:string;evidence:{explanation:string;proposed_changes:Record<string,{before:unknown;after:unknown}>;preserved_destination_fields:Record<string,unknown>;authority_decisions:Record<string,{owner:string;authorized:boolean;destination_field:string}>;conflict_reasons:string[];source_identity:{external_id:string};destination_identity:{external_id:string};destination_digest:string;source_updated_at:string|null;destination_updated_at:string|null;mapping_version_id:string;configuration_version_id:string}};
export type SyncExecution = {id:string;plan_id:string;state:string;workflow_execution_id:string|null;workflow_state?:string|null;workflow_error_category?:string|null;created_at:string};
export type SyncConflict = {id:string;category:string;status:string;revision:number;plan_id:string};
export type SyncOperation = {id:string;outcome:string;action_key:string;result:unknown};
export type ReconciliationRun = {id:string;summary:{counts:Record<string,number>;finding_count:number;source_records_scanned:number;read_only:boolean};created_at:string};
export type ReconciliationFinding = {id:string;category:string;source_external_id:string|null;evidence:unknown};

export type SyncStatus={executions:Record<string,number>;conflicts:Record<string,number>;enabled_configurations:number;basis:string};
