import {request} from '@playwright/test';
import {readFileSync,chmodSync,mkdirSync} from 'node:fs';
export default async function setup(){
 mkdirSync('.local',{recursive:true});
 const fixture=JSON.parse(readFileSync('../backend/.local/phase11-fixtures.json','utf8'));
 const client=await request.newContext({baseURL:'http://127.0.0.1:3000'});
 async function post(path:string,data:unknown){const csrf=await (await client.get('/api/platform/auth/csrf')).json();const r=await client.post('/api/platform/'+path,{data,headers:{Origin:'http://127.0.0.1:3000','X-CSRF-Token':csrf.csrf_token}});if(!r.ok())throw Error('Fixture authentication failed '+r.status());return r.json();}
 await post('auth/login',{email:fixture.email,password:fixture.password});await post('tenant/switch',{tenant_id:fixture.alpha});
 await client.storageState({path:'.local/phase11-state.json'});chmodSync('.local/phase11-state.json',0o600);await client.dispose();
}
