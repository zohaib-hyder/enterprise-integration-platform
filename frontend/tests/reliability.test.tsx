import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { api, ApiError } from '../lib/api';
import { AuthGuard, AuthProvider } from '../components/auth-provider';
import ConnectionReliability from '../components/connection-reliability';
import Alerts from '../components/alerts';
vi.mock('next/navigation',() => ({useRouter:() => ({replace:vi.fn()})}));
vi.mock('../lib/api',async importOriginal => ({...await importOriginal<typeof import('../lib/api')>(),api:vi.fn()}));
const me={user:{id:'user',display_name:'Synthetic',email:'engineer@example.test'},tenants:[{id:'tenant',name:'Synthetic',role:'INTEGRATION_ENGINEER'}],active_tenant_id:'tenant',permissions:['operations:read','integrations:manage','alerts:acknowledge','alerts:resolve']};
const policy={schema_version:1,connect_timeout_ms:1000,read_timeout_ms:3000,operation_deadline_ms:5000,max_attempts:3,base_delay_seconds:1,max_delay_seconds:30,jitter_fraction:.2,circuit_failure_threshold:5,circuit_cooldown_seconds:30,repeated_failure_threshold:3,credential_mode:'OPTIONAL'};
const snapshot={connection_id:'connection',health_status:'UNAVAILABLE',last_checked_at:'2030-01-15T12:00:00Z',policy,policy_configured:true,one_off_attempts:1,rate_limits:[{scope:'CONNECTION',limit:2,used:2,remaining:0,window_seconds:60,reset_at:'2030-01-15T12:00:30Z',retry_after_seconds:30}],circuit:{state:'OPEN',failure_count:3,open_until:'2030-01-15T12:00:30Z',probe_expires_at:null},failure_injection:{enabled:false,scenario:'NONE',remaining:0,consumed:0}};
const credential={id:'credential',credential_type:'API_KEY',generation:1,status:'ACTIVE',masked_value:'••••••••',key_reference:'local-v1',expires_at:null};
const alert={id:'alert',category:'CIRCUIT_OPEN',severity:'WARNING',status:'OPEN',correlation_id:'correlation',context:{connection_id:'connection',reason:'HTTP_503'},occurrence_count:2,last_seen_at:'2030-01-15T12:00:00Z'};
const metrics={since:'2030-01-15T00:00:00Z',checked_at:'2030-01-15T12:00:00Z',operation_count:4,admitted_operation_count:3,success_count:1,failure_count:2,deferred_count:1,retry_count:2,operation_retry_count:1,rate_limit_count:1,circuit_transition_count:2,credential_refresh_count:1,credential_refresh_success_count:1,latency_ms:{average:12,maximum:24},workflow_backlog:{RETRY_SCHEDULED:1},dead_letter_count:0,worker_lease_recovery_count:0,connection_health:{UNAVAILABLE:1},retention:{days:7,maximum_rows_per_tenant:5000},basis:'Retained committed observations'};
const page=(items:unknown[]) => ({items,total:items.length,offset:0,limit:25});
let alertStatus='OPEN';
function defaultApi(path:string):unknown {
  if(path==='auth/me')return me;
  if(path==='operations/metrics')return metrics;
  if(path==='connections/connection/reliability')return snapshot;
  if(path.includes('/credentials?'))return page([credential]);
  if(path.endsWith('/credentials') || path.endsWith('/rotate'))return credential;
  if(path.endsWith('/revoke'))return {...credential,status:'REVOKED'};
  if(path.includes('/circuit/history'))return page([{from_state:'CLOSED',to_state:'OPEN',reason:'HTTP_503',created_at:'2030-01-15T12:00:00Z'}]);
  if(path.startsWith('alerts?'))return page([{...alert,status:alertStatus}]);
  if(path==='alerts/alert')return {...alert,status:alertStatus};
  if(path.includes('/history?'))return page([{id:'history',event_type:'OPENED',actor_id:null,correlation_id:'correlation',created_at:'2030-01-15T12:00:00Z'}]);
  if(path.endsWith('/acknowledge')){alertStatus='ACKNOWLEDGED';return {...alert,status:alertStatus};}
  if(path.endsWith('/resolve')){alertStatus='RESOLVED';return {...alert,status:alertStatus};}
  return {};
}
beforeEach(() => {alertStatus='OPEN';vi.mocked(api).mockReset();vi.mocked(api).mockImplementation(async path => defaultApi(path));});
afterEach(() => cleanup());
function view(component:React.ReactNode){return render(<AuthProvider><AuthGuard>{component}</AuthGuard></AuthProvider>);}
function panel(){return view(<ConnectionReliability connectionId="connection" connectorType="synthetic_crm" />);}

test('connection health, circuit and quotas reflect persisted state',async () => {panel();await screen.findByText('UNAVAILABLE');expect(screen.getByText('OPEN')).toBeInTheDocument();expect(screen.getByText('2 / 2 used · 0 remaining')).toBeInTheDocument();expect(screen.getByText('Retry after 30s')).toBeInTheDocument();});
test('reliability settings use protected PUT and bounded fields',async () => {panel();await screen.findByLabelText('Read timeout (ms)');fireEvent.change(screen.getByLabelText('Read timeout (ms)'),{target:{value:'2000'}});fireEvent.click(screen.getByRole('button',{name:'Save reliability policy'}));await waitFor(() => expect(api).toHaveBeenCalledWith('connections/connection/reliability',{method:'PUT',data:{...policy,read_timeout_ms:2000}}));});
test('quota configuration is explicit and tenant scoped',async () => {panel();await screen.findByLabelText('Operation limit');fireEvent.change(screen.getByLabelText('Operation limit'),{target:{value:'3'}});fireEvent.click(screen.getByRole('button',{name:'Save operation budget'}));await waitFor(() => expect(api).toHaveBeenCalledWith('connections/connection/rate-limits',{method:'PUT',data:{scope:'CONNECTION',limit:3,window_seconds:60}}));});
test('one-time credential submission clears secret before the response',async () => {panel();await screen.findByLabelText('Synthetic secret value');const field=screen.getByLabelText('Synthetic secret value') as HTMLInputElement;fireEvent.change(field,{target:{value:'sim_transient_submission_value'}});fireEvent.click(screen.getByRole('button',{name:'Store encrypted credential'}));expect(field.value).toBe('');await screen.findByText(/Credential stored encrypted/);expect(api).toHaveBeenCalledWith('connections/connection/credentials',{method:'POST',data:{credential_type:'API_KEY',value:'sim_transient_submission_value',expires_at:null}});expect(screen.queryByText('sim_transient_submission_value')).not.toBeInTheDocument();});
test('credential rotation and revocation never reveal saved values',async () => {panel();fireEvent.click(await screen.findByRole('button',{name:'Replace credential'}));fireEvent.change(screen.getByLabelText('Synthetic secret value'),{target:{value:'sim_replacement_fixture_value'}});fireEvent.click(screen.getByRole('button',{name:'Rotate credential'}));await waitFor(() => expect(api).toHaveBeenCalledWith('connections/connection/credentials/credential/rotate',{method:'POST',data:{value:'sim_replacement_fixture_value',expires_at:null}}));fireEvent.click(screen.getByRole('button',{name:'Revoke credential'}));await waitFor(() => expect(api).toHaveBeenCalledWith('connections/connection/credentials/credential/revoke',{method:'POST'}));expect(screen.getByText(/••••••••/)).toBeInTheDocument();});
test('failure injection requires explicit enabling and bounded attempts',async () => {panel();await screen.findByLabelText('Enable synthetic fault');fireEvent.click(screen.getByLabelText('Enable synthetic fault'));fireEvent.change(screen.getByLabelText('Fault scenario'),{target:{value:'HTTP_429'}});fireEvent.click(screen.getByRole('button',{name:'Save failure injection'}));await waitFor(() => expect(api).toHaveBeenCalledWith('connections/connection/failure-injection',{method:'PUT',data:{enabled:true,scenario:'HTTP_429',attempts:1,ttl_seconds:300,retry_after:'1'}}));});
test('actual metrics have no fabricated zero latency sample',async () => {panel();await screen.findByRole('region',{name:'Operational metrics'});expect(screen.getByText(/12 ms average · 24 ms maximum/)).toBeInTheDocument();expect(screen.getByText(/RETRY_SCHEDULED: 1/)).toBeInTheDocument();});
test('operations users inspect but cannot manage policies credentials or faults',async () => {vi.mocked(api).mockImplementation(async path => path==='auth/me' ? {...me,permissions:['operations:read','alerts:acknowledge']} : defaultApi(path));panel();await screen.findByText('UNAVAILABLE');expect(screen.queryByRole('button',{name:'Save reliability policy'})).not.toBeInTheDocument();expect(screen.queryByLabelText('Synthetic secret value')).not.toBeInTheDocument();expect(screen.queryByLabelText('Enable synthetic fault')).not.toBeInTheDocument();});
test('alerts are filtered by severity and status',async () => {view(<Alerts />);await screen.findByRole('button',{name:/CIRCUIT_OPEN/});fireEvent.change(screen.getByLabelText('Alert severity'),{target:{value:'CRITICAL'}});fireEvent.change(screen.getByLabelText('Alert status'),{target:{value:'OPEN'}});await waitFor(() => expect(api).toHaveBeenCalledWith('alerts?offset=0&limit=25&status=OPEN&severity=CRITICAL',expect.anything()));});
test('alert inspection acknowledgement and resolution preserve lifecycle visibility',async () => {view(<Alerts />);fireEvent.click(await screen.findByRole('button',{name:/CIRCUIT_OPEN/}));await screen.findByRole('region',{name:'Alert details'});expect(screen.getByText(/HTTP_503/)).toBeInTheDocument();fireEvent.click(screen.getByRole('button',{name:'Acknowledge alert'}));await screen.findByText('Alert acknowledged');fireEvent.click(screen.getByRole('button',{name:'Resolve alert'}));await screen.findByText(/Alert resolved/);expect(api).toHaveBeenCalledWith('alerts/alert/acknowledge',{method:'POST'});expect(api).toHaveBeenCalledWith('alerts/alert/resolve',{method:'POST'});});
test('read-only inspection does not show alert management actions',async () => {vi.mocked(api).mockImplementation(async path => path==='auth/me' ? {...me,permissions:['operations:read']} : defaultApi(path));view(<Alerts />);fireEvent.click(await screen.findByRole('button',{name:/CIRCUIT_OPEN/}));await screen.findByRole('region',{name:'Alert details'});expect(screen.queryByRole('button',{name:'Acknowledge alert'})).not.toBeInTheDocument();});
test('empty alerts and no metric samples remain honest',async () => {vi.mocked(api).mockImplementation(async path => path.startsWith('alerts?') ? page([]) : path==='operations/metrics' ? {...metrics,latency_ms:{average:null,maximum:null}} : defaultApi(path));view(<Alerts />);await screen.findByText('No alerts match this selection.');expect(screen.getByText('Operation latency: No samples')).toBeInTheDocument();});
test('loading state waits for real responses',async () => {vi.mocked(api).mockImplementation(async path => path==='auth/me' ? me : new Promise(() => {}));panel();await screen.findByText('Loading reliability controls…');});
test('reliability error is visible and controls do not fabricate health',async () => {vi.mocked(api).mockImplementation(async path => {if(path==='connections/connection/reliability')throw new ApiError(403,'Permission denied');return defaultApi(path);});panel();await screen.findByRole('alert');expect(screen.getByText('Permission denied')).toBeInTheDocument();expect(screen.queryByText('UNAVAILABLE')).not.toBeInTheDocument();});

test('operations users can acknowledge alerts without receiving resolution controls',async () => {
  vi.mocked(api).mockImplementation(async path => path==='auth/me' ? {...me,permissions:['operations:read','alerts:acknowledge']} : defaultApi(path));
  view(<Alerts />);fireEvent.click(await screen.findByRole('button',{name:/CIRCUIT_OPEN/}));
  await screen.findByRole('region',{name:'Alert details'});
  expect(screen.getByRole('button',{name:'Acknowledge alert'})).toBeEnabled();
  expect(screen.queryByRole('button',{name:'Resolve alert'})).not.toBeInTheDocument();
});
test('missing operations permission shows an unauthorized state without requests for tenant metrics',async () => {
  vi.mocked(api).mockImplementation(async path => path==='auth/me' ? {...me,permissions:[]} : defaultApi(path));
  view(<Alerts />);await screen.findByRole('alert');
  expect(screen.getByText(/do not have permission to inspect/)).toBeInTheDocument();
  expect(vi.mocked(api).mock.calls.some(([path]) => path==='operations/metrics')).toBe(false);
});
