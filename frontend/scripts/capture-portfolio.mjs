/** Real app screenshots only. No routes mocked, no browser state written to disk. */
import {chromium, expect as baseExpect} from '@playwright/test';
import {readFileSync, writeFileSync, mkdirSync} from 'node:fs';
import {resolve, relative} from 'node:path';
import {createHash} from 'node:crypto';
import sharp from 'sharp';

const root = resolve('..');
const envPath = resolve(process.argv[2] ?? '../.local/phase13a/release.env');
if (!relative(resolve(root, '.local'), envPath).startsWith('..') && envPath.endsWith('.env')) {
  // The private file is parsed in memory and is never emitted.
} else throw new Error('Use a private repository .local env file');
const env = Object.fromEntries(readFileSync(envPath, 'utf8').trim().split('\n').map(line => {
  const index = line.indexOf('='); return [line.slice(0,index), line.slice(index+1)];
}));
if (env.APP_ENV !== 'test' || env.AI_OPENAI_ENABLED !== 'false') throw new Error('Mock test mode required');
const fixtures = JSON.parse(readFileSync(resolve(root,'docs/evidence/phase13a-screenshot-fixtures.json'),'utf8'));
const output = resolve(root,'docs/assets/screenshots'); mkdirSync(output,{recursive:true});
const browser = await chromium.launch({channel:'chromium'});
const context = await browser.newContext({baseURL:'http://127.0.0.1:3300',viewport:{width:1440,height:1100},locale:'en-US',timezoneId:'America/Toronto',reducedMotion:'reduce'});
const page = await context.newPage();
const expect = baseExpect.configure({timeout:60000});
page.setDefaultTimeout(60000);
const manifest = [];
const errors = [];
page.on('pageerror',()=>errors.push('Browser application error'));
page.on('response',r=>{if(r.url().includes('/api/platform/') && r.status()>=500) errors.push('API server error');});
async function post(path,data){
  const csrf=await (await page.request.get('/api/platform/auth/csrf')).json();
  const response=await page.request.post('/api/platform/'+path,{data,headers:{Origin:'http://127.0.0.1:3300','X-CSRF-Token':csrf.csrf_token}});
  if(!response.ok()) throw new Error('Fixture authentication failed'); return response.json();
}
async function open(path){
  await page.goto(path); await expect(page.getByLabel('Organization',{exact:true})).toBeVisible();
  await page.waitForLoadState('networkidle');
  await expect(page.getByRole('status').filter({hasText:/^Loading/})).toHaveCount(0);
}
async function shot(name,route,caption){
  await page.waitForLoadState('networkidle'); await page.evaluate(()=>document.fonts.ready);
  const text = await page.locator('body').innerText();
  for(const [key,value] of Object.entries(env)){
    if(/PASSWORD|SECRET|ENCRYPTION_KEY/.test(key) && value.length>=12 && text.includes(value)) throw new Error('Sensitive value on capture surface');
  }
  if(/\/(?:Users|home)\/[a-z0-9_.-]+\//i.test(text)) throw new Error('Personal path/name on capture surface');
  if(errors.length) throw new Error('Application failed during capture');
  await expect(page.getByRole('status').filter({hasText:/^Loading/})).toHaveCount(0);
  const raw=await page.screenshot({animations:'disabled',fullPage:false});
  const png=await sharp(raw).png({compressionLevel:9,adaptiveFiltering:true}).toBuffer();
  writeFileSync(resolve(output,name+'.png'),png);
  manifest.push({file:name+'.png',route,caption,width:1440,height:1100,bytes:png.length,sha256:createHash('sha256').update(png).digest('hex')});
  console.log('Captured '+name);
}
try {
  await page.goto('/login');
  await post('auth/login',{email:env.BOOTSTRAP_EMAIL,password:env.BOOTSTRAP_PASSWORD});
  await post('tenant/switch',{tenant_id:fixtures.tenant_id});
  await open('/'); await shot('01-dashboard','/','Overview with real API health and clearly labeled local simulator capabilities.');
  await open('/connections');
  await page.getByRole('button',{name:/Demo CRM ENABLED/}).click();
  await expect(page.getByRole('button',{name:'Test connection',exact:true})).toBeEnabled();
  await expect(page.getByText('Loading synthetic records…',{exact:true})).toHaveCount(0);
  await expect(page.getByText('Loading reliability controls…',{exact:true})).toHaveCount(0);
  await shot('02-connections','/connections','Configured CRM simulator and a real local health check.');
  await open('/events'); await page.locator('tbody button').first().click();
  await expect(page.getByRole('region',{name:'Event details'})).toBeVisible();
  await shot('03-events','/events','Accepted signed synthetic event and durable ingestion provenance.');
  await open('/workflows'); await shot('04-workflows','/workflows','Persisted versioned workflows; no invented provider integration.');
  await open('/executions'); await page.locator('#main-content button').filter({hasText:'SUCCEEDED'}).first().click();
  await expect(page.getByRole('region',{name:'Execution details'})).toBeVisible();
  await page.getByRole('region',{name:'Execution details'}).scrollIntoViewIfNeeded();
  await shot('05-executions','/executions','Actual successful synthetic workflow with checkpoint history.');
  await open('/synchronization');
  await Promise.all([page.waitForResponse(r=>r.url().includes('/api/platform/sync/executions?')&&r.status()===200),page.getByRole('button',{name:'Executions',exact:true}).click()]);
  await expect(page.locator('#main-content').getByText('SUCCEEDED',{exact:false}).first()).toBeVisible();
  await shot('06-synchronization','/synchronization','Committed sync decision and genuine held update from an independent ATS edit.');
  await page.getByRole('button',{name:'Conflicts',exact:true}).click();
  await page.getByRole('button',{name:/DESTINATION_CHANGED/}).first().click();
  await expect(page.getByLabel('Resolution reason',{exact:true})).toBeVisible();
  await shot('07-conflict-review','/synchronization','Open destination-change conflict with before/after authority evidence; unresolved.');
  await open('/notifications');
  await page.getByRole('button',{name:'Deliveries',exact:true}).click();
  await expect(page.locator('tbody').getByText('SUCCEEDED',{exact:false}).first()).toBeVisible();
  await shot('08-notifications','/notifications','Separate synthetic Slack/email delivery receipts after targeted retry.');
  await open('/calendar'); await shot('09-calendar','/calendar','Synthetic calendar coordination; no real invitation sent.');
  await open('/operations'); await shot('10-operations','/operations','Observed tenant evidence and failure history from local fixtures.');
  await page.getByRole('button',{name:'Timeline',exact:true}).click();
  await page.getByLabel('Correlation ID',{exact:true}).fill(fixtures.correlation_id);
  await page.getByRole('button',{name:'Load correlated timeline',exact:true}).click();
  await expect(page.getByRole('region',{name:'Unified operational timeline'}).locator('li').first()).toBeVisible();
  await shot('11-incident-timeline','/operations','Real correlated incident/workflow timeline with persisted provenance.');
  await open('/ai-insights'); await page.getByRole('button',{name:'Inspect analysis',exact:true}).first().click();
  await expect(page.getByLabel('Authorized evidence')).toBeVisible();
  await expect(page.getByRole('button',{name:'Save AI configuration',exact:true})).toBeEnabled();
  await page.getByRole('region',{name:'Structured AI explanation'}).scrollIntoViewIfNeeded();
  await shot('12-ai-insights','/ai-insights','SIMULATED deterministic mock analysis with authorized citations.');
  await open('/ai-review');
  await expect(page.getByRole('region',{name:'Review queue'})).toContainText('ESCALATE_FOR_MANUAL_REVIEW');
  await page.getByRole('button',{name:/inspect evidence/}).first().click();
  await expect(page.getByLabel('Reviewer comment',{exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'Accept for preview',exact:true}).first()).toBeVisible();
  await page.getByLabel('Reviewer comment',{exact:true}).fill('Synthetic portfolio reviewer inspects the cited evidence; no action is executed.');
  await expect(page.getByRole('button',{name:'Accept for preview',exact:true}).first()).toBeEnabled();
  await page.locator('textarea').first().scrollIntoViewIfNeeded();
  await shot('13-human-review','/ai-review','Human review controls; accepting creates a preview only, not execution.');
  await post('auth/logout',{});
  writeFileSync(resolve(output,'manifest.json'),JSON.stringify({recorded_at_utc:new Date().toISOString(),scope:'Unaltered real local UI; synthetic fixtures; mock AI; lossless PNG compression; no browser chrome or persisted session state',screenshots:manifest},null,2)+'\n');
} catch(error) {await page.screenshot({path:resolve(root,'.local/phase13a/capture-error.png')});writeFileSync(resolve(root,'.local/phase13a/capture-error.txt'),await page.locator('body').innerText());throw error;} finally {await context.close(); await browser.close();}
