import {render,screen,fireEvent,cleanup,waitFor} from '@testing-library/react';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import Notifications from '../components/notifications';
import {api} from '../lib/api';
const auth=vi.hoisted(()=>({permissions:['operations:read','integrations:manage','executions:recover']}));
vi.mock('../components/auth-provider',()=>({useAuth:()=>({me:{permissions:auth.permissions}})}));
vi.mock('../lib/api',()=>({api:vi.fn()}));
const page=(items:unknown[])=>({items,total:items.length,offset:0,limit:25});
const delivery={id:'delivery',channel:'SLACK',status:'RETRY_SCHEDULED',attempt_count:1,created_at:'2026-01-01T00:00:00Z',correlation_id:'corr',intent_id:'intent'};
async function response(path:string){
 if(path.startsWith('notifications/templates?'))return page([{id:'template',name:'Recruiting template',channel:'SLACK',active_version:1}]);
 if(path.includes('/templates/template/versions'))return page([{id:'version',version:1,subject:'',body:'Hello {job_title}'}]);
 if(path==='notifications/templates/preview')return {subject:'',body:'Hello Synthetic engineer'};
 if(path.startsWith('notifications/rules?'))return page([{id:'rule',name:'Recruiting rule',enabled:true,active_version:1,revision:1}]);
 if(path.includes('/rules/rule/versions'))return page([{id:'rv',version:1,trigger:'SYNC_CREATE',destination:'recruiting',template_version_id:'version'}]);
 if(path.startsWith('connections?'))return page([{id:'slack',name:'Slack',connector_type:'synthetic_slack',public_config:{allowed_channels:['recruiting']}}]);
 if(path.startsWith('sync/configurations?'))return page([]);
 if(path.startsWith('notifications/deliveries?'))return page([delivery]);
 if(path==='notifications/deliveries/delivery')return delivery;
 if(path.endsWith('/attempts?limit=100'))return page([{id:'attempt',number:1,status:'RETRY_SCHEDULED',error_category:'TEMPORARY_UNAVAILABLE',retry_after:7}]);
 if(path.endsWith('/receipts?limit=100'))return page([]);
 if(path==='notifications/metrics')return {counts:[{channel:'SLACK',status:'RETRY_SCHEDULED',count:1}]};
 if(path.endsWith('/review'))return {resolved:false,message:'No verified provider receipt; resend remains blocked'};
 return {};
}
beforeEach(()=>{auth.permissions=['operations:read','integrations:manage','executions:recover'];delivery.status='RETRY_SCHEDULED';vi.mocked(api).mockImplementation(response as typeof api);});afterEach(()=>{cleanup();vi.clearAllMocks();});
test('overview reflects real persisted counts',async()=>{render(<Notifications/>);await screen.findByText('SLACK · RETRY_SCHEDULED');expect(screen.getByText('1')).toBeInTheDocument();});
test('template history renders preview and activates pinned version',async()=>{render(<Notifications/>);fireEvent.click(screen.getByRole('button',{name:'Templates'}));fireEvent.click(await screen.findByRole('button',{name:/Recruiting template/}));fireEvent.click(await screen.findByRole('button',{name:'Preview rendering'}));await screen.findByText('Hello Synthetic engineer');fireEvent.click(screen.getByRole('button',{name:'Activate template version'}));await screen.findByText('Template version activated');expect(api).toHaveBeenCalledWith('notifications/templates/template/activate',{method:'POST',data:{version_id:'version'}});});
test('creates immutable template version',async()=>{render(<Notifications/>);fireEvent.click(screen.getByRole('button',{name:'Templates'}));fireEvent.click(await screen.findByRole('button',{name:/Recruiting template/}));fireEvent.change(await screen.findByLabelText('Template body'),{target:{value:'Updated {job_title}'}});fireEvent.click(screen.getByRole('button',{name:'Create immutable template version'}));await screen.findByText('Immutable template version created');});
test('rule enable disable uses optimistic revision',async()=>{render(<Notifications/>);fireEvent.click(screen.getByRole('button',{name:'Rules'}));fireEvent.click(await screen.findByRole('button',{name:'Disable rule Recruiting rule'}));await screen.findByText('Rule activation updated');expect(api).toHaveBeenCalledWith('notifications/rules/rule',{method:'PATCH',data:{expected_revision:1,enabled:false}});});
test('rule history is inspectable',async()=>{render(<Notifications/>);fireEvent.click(screen.getByRole('button',{name:'Rules'}));fireEvent.click(await screen.findByRole('button',{name:/Recruiting rule · Enabled/}));await screen.findByText(/v1: SYNC_CREATE/);});
async function inspect(){render(<Notifications/>);fireEvent.click(screen.getByRole('button',{name:'Deliveries'}));fireEvent.click(await screen.findByRole('button',{name:'Inspect delivery delivery'}));await screen.findByRole('region',{name:'Delivery timeline'});}
test('delivery timeline and Retry After',async()=>{await inspect();expect(screen.getByText(/Retry-After 7s/)).toBeInTheDocument();});
test('retry requires a reason and targets only delivery',async()=>{await inspect();expect(screen.getByRole('button',{name:'Retry eligible delivery'})).toBeDisabled();fireEvent.change(screen.getByLabelText('Recovery reason'),{target:{value:'Retry synthetic delivery'}});fireEvent.click(screen.getByRole('button',{name:'Retry eligible delivery'}));await screen.findByText('Delivery recovery recorded');expect(api).toHaveBeenCalledWith('notifications/deliveries/delivery/retry',{method:'POST',data:{reason:'Retry synthetic delivery'}});});
test('cancel action persists recovery',async()=>{await inspect();fireEvent.change(screen.getByLabelText('Recovery reason'),{target:{value:'Cancel synthetic delivery'}});fireEvent.click(screen.getByRole('button',{name:'Cancel delivery'}));await screen.findByText('Delivery recovery recorded');expect(api).toHaveBeenCalledWith('notifications/deliveries/delivery/cancel',{method:'POST',data:{reason:'Cancel synthetic delivery'}});});
test('unknown outcome allows receipt review with no resend',async()=>{delivery.status='UNKNOWN';await inspect();expect(screen.queryByRole('button',{name:'Retry eligible delivery'})).not.toBeInTheDocument();fireEvent.change(screen.getByLabelText('Recovery reason'),{target:{value:'Inspect missing checkpoint'}});fireEvent.click(screen.getByRole('button',{name:'Check provider receipt'}));await screen.findByText('No verified provider receipt; resend remains blocked');});
test('channel and status filters alter query',async()=>{render(<Notifications/>);fireEvent.click(screen.getByRole('button',{name:'Deliveries'}));await screen.findByLabelText('Channel filter');fireEvent.change(screen.getByLabelText('Channel filter'),{target:{value:'EMAIL'}});await waitFor(()=>expect(api).toHaveBeenCalledWith(expect.stringContaining('&channel=EMAIL'),expect.anything()));});
test('operations cannot edit templates or rules',async()=>{auth.permissions=['operations:read','executions:recover'];render(<Notifications/>);fireEvent.click(screen.getByRole('button',{name:'Templates'}));await screen.findByRole('button',{name:/Recruiting template/});expect(screen.queryByRole('button',{name:'Create template'})).not.toBeInTheDocument();fireEvent.click(screen.getByRole('button',{name:'Rules'}));expect(screen.queryByRole('button',{name:/Disable rule/})).not.toBeInTheDocument();});
test('loading and error states',async()=>{vi.mocked(api).mockRejectedValue(new Error('Database unavailable'));render(<Notifications/>);expect(screen.getByRole('status')).toHaveTextContent('Loading');await screen.findByRole('alert');expect(screen.getByRole('alert')).toHaveTextContent('Database unavailable');});
test('unauthorized page avoids requests',()=>{auth.permissions=[];render(<Notifications/>);expect(screen.getByRole('alert')).toHaveTextContent('Permission required');expect(api).not.toHaveBeenCalled();});

test('creates a disabled rule pinned to an activated template and destination',async()=>{
 render(<Notifications/>);fireEvent.click(screen.getByRole('button',{name:'Rules'}));await screen.findByRole('option',{name:'Slack'});
 fireEvent.change(screen.getByLabelText('Rule name'),{target:{value:'New synthetic rule'}});
 fireEvent.change(screen.getByLabelText('Notification connection'),{target:{value:'slack'}});
 fireEvent.change(screen.getByLabelText('Allowlisted destination'),{target:{value:'recruiting'}});
 fireEvent.change(screen.getByLabelText('Rule template'),{target:{value:'template'}});
 await screen.findByRole('option',{name:'Version 1'});
 fireEvent.change(screen.getByLabelText('Pinned template version'),{target:{value:'version'}});
 fireEvent.click(screen.getByRole('button',{name:'Save rule version'}));await screen.findByText('Rule version saved');
 expect(api).toHaveBeenCalledWith('notifications/rules',{method:'POST',data:{name:'New synthetic rule',enabled:false,trigger:'SYNC_CREATE',connection_id:'slack',template_version_id:'version',destination:'recruiting',sync_configuration_id:null}});
});

test('manual selection excludes enabled sync rules',async()=>{
 render(<Notifications/>);await screen.findByText('SLACK · RETRY_SCHEDULED');fireEvent.click(screen.getByRole('button',{name:'Manual'}));
 await waitFor(()=>expect(api).toHaveBeenCalledWith('notifications/rules/rule/versions?limit=100',expect.anything()));
 expect(screen.queryByRole('option',{name:'Recruiting rule'})).not.toBeInTheDocument();
 expect(screen.getByRole('button',{name:'Queue manual notification'})).toBeDisabled();
});
