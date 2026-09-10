import {readTaskEnvelope} from '../src/task-presentation.js';
import { describe, it, expect, vi } from 'vitest';
import { readContext, enrichParticipantNames } from '../src/slack-context.js';
import { buildEnvelope, focusContext, messageFingerprint, compareTs, serializeEnvelope, type ThreadContext } from '../src/context-envelope.js';
import { routeSlackThreadEvent, type SlackThreadEvent, type ThreadRouterConfig } from '../src/thread-router.js';
import { MemoryThreadStore } from '../src/thread-store.js';

const event: SlackThreadEvent = { teamId:'T1', channelId:'D1', threadTs:'2000.000001', messageTs:'2100.000001', senderUserId:'U1', text:'<@U2> now', mention:{type:'user',id:'U2'} };
const msg = (ts: string, text = ts) => ({ ts, text, user:'U1' });
const empty = (): ThreadContext => ({ anchorTs:event.threadTs, cutoffTs:event.messageTs, capturedAt:'fixed', timeline:{status:'complete',messages:[]} });
const decode = (s: string): any => readTaskEnvelope(s);
describe('tree Slack context', () => {
  it('refreshes history at each mention, expands sibling threads and retains an old current root once', async () => {
    const e={...event,threadTs:'100.000001',messageTs:'90000.000001'};
    const f=vi.fn<typeof fetch>().mockImplementation(async input=>{
      const u=new URL(String(input));expect(u.searchParams.get('latest')).toBe(e.messageTs);
      if(u.pathname.endsWith('history')) {expect(u.searchParams.get('oldest')).toBe('3600.000001');return Response.json({ok:true,messages:[{...msg('89000.000001'),reply_count:2},msg('3599.000001'),{...msg('89500.000001'),thread_ts:'89000.000001'}]});}
      const ts=u.searchParams.get('ts')!;
      return Response.json({ok:true,messages:[msg(ts),{...msg(ts==='100.000001'?'89900.000001':'89600.000001'),thread_ts:ts},msg('90001.000001')]});
    });
    const c=await readContext(e,'test',f);
    expect(c.timeline.messages.map(m=>m.ts)).toEqual(['100.000001','89000.000001']);
    expect(c.timeline.messages[1]!.replies!.messages.map(m=>m.ts)).toEqual(['89600.000001']);
    const envelope=JSON.parse(buildEnvelope(e,c));expect(envelope.schemaVersion).toBe(4);
    expect(envelope.context.timeline.messages[0].replies.messages.at(-1).currentRequest).toBe(true);
    expect(envelope.context.timeline.messages[0].replies.messages.at(-1).text).toBe('');
    expect(f).toHaveBeenCalledTimes(3);
  });
  it('selects the newest 40 roots and deduplicates a current root within the window',async()=>{
    const e={...event,messageTs:event.threadTs};
    const f=vi.fn<typeof fetch>().mockResolvedValue(Response.json({ok:true,messages:Array.from({length:45},(_,i)=>msg(`${2000-i}.000001`))}));
    const c=await readContext(e,'test',f);expect(c.timeline.messages).toHaveLength(40);
    expect(c.timeline.messages.filter(m=>m.ts===e.threadTs)).toHaveLength(1);expect(c.timeline.status).toBe('truncated');
  });
  it('includes sparse older messages up to 24 hours without an extra history pass',async()=>{
    const e={...event,threadTs:'100000.000001',messageTs:'100000.000001'};
    const f=vi.fn<typeof fetch>().mockImplementation(async input=>{
      const url=new URL(String(input));expect(url.pathname).toBe('/api/conversations.history');
      expect(url.searchParams.get('oldest')).toBe('13600.000001');
      expect(url.searchParams.get('latest')).toBe(e.messageTs);
      return Response.json({ok:true,messages:[msg('100001.000001'),msg(e.messageTs),msg('99500.000001'),msg('92800.000001'),msg('13600.000001'),msg('13600.000000')]});
    });
    const c=await readContext(e,'test',f);
    expect(c.timeline.messages.map(m=>m.ts)).toEqual(['13600.000001','92800.000001','99500.000001',e.messageTs]);
    expect(f).toHaveBeenCalledTimes(1);
  });
  it('marks inaccessible sibling replies without losing current conversation',async()=>{
    const e={...event,messageTs:event.threadTs};
    const f:typeof fetch=async input=>String(input).includes('history')?Response.json({ok:true,messages:[{...msg('1999.000001'),reply_count:1}]}):Response.json({ok:false,error:'missing_scope'});
    const c=await readContext(e,'test',f);expect(c.timeline.messages[0]!.replies!.status).toBe('unavailable');expect(c.timeline.messages.at(-1)!.ts).toBe(e.threadTs);
  });
  it('expands only the five most recently active side branches',async()=>{
    const e={...event,messageTs:event.threadTs};
    const f:typeof fetch=async input=>{
      const u=new URL(String(input));const ts=u.searchParams.get('ts');
      return Response.json({ok:true,messages:ts?[msg(ts),...Array.from({length:30},(_,i)=>({...msg(`1999.${String(i+1).padStart(6,'0')}`),thread_ts:ts}))]:Array.from({length:15},(_,i)=>({...msg(`${1900+i}.000001`),reply_count:30}))});
    };
    const c=await readContext(e,'test',f);const sides=c.timeline.messages.filter(m=>m.ts!==e.threadTs&&m.replies);
    expect(sides).toHaveLength(5);expect(sides.flatMap(m=>m.replies?.messages??[])).toHaveLength(100);
    expect(sides[0]!.replies!.reason).toBe('thread_message_limit');
  });
  it('prioritizes a recently active older root before a newer inactive root',async()=>{
    const e={...event,messageTs:event.threadTs};const readRoots:string[]=[];
    const f:typeof fetch=async input=>{
      const u=new URL(String(input));const ts=u.searchParams.get('ts');
      if(ts){readRoots.push(ts);return Response.json({ok:true,messages:[msg(ts)]});}
      return Response.json({ok:true,messages:[
        {...msg('1900.000001'),reply_count:1,latest_reply:'1999.000001'},
        {...msg('1950.000001'),reply_count:1,latest_reply:'1951.000001'},
      ]});
    };
    await readContext(e,'test',f);expect(readRoots).toEqual(['1900.000001','1950.000001']);
  });
  it('marks optional rate limits and does not leak attachment URLs',async()=>{
    const e={...event,messageTs:event.threadTs};
    const f:typeof fetch=async input=>String(input).includes('history')?Response.json({ok:true,messages:[{...msg('1999.000001'),reply_count:1,files:[{id:'F1',name:'photo.png',mimetype:'image/png',url_private:'https://private.test'}]}]}):new Response('',{status:429});
    const c=await readContext(e,'test',f);expect(c.timeline.messages[0]!.replies!.reason).toBe('rate_limited');
    expect(c.timeline.messages[0]!.files[0]!.contentStatus).toBe('not_loaded');expect(JSON.stringify(c)).not.toContain('private.test');
  });
  it('detects edits beyond the displayed text and attachment limits without exposing the internal hash',async()=>{
    const base={...event,messageTs:event.threadTs,text:'x'.repeat(5000)+'a',files:Array.from({length:6},(_,i)=>({id:`F${i}`,name:`${i}.png`,mimetype:'image/png'}))};
    const first=await readContext(base,'test',async()=>Response.json({ok:true,messages:[]}));
    const changedText=await readContext({...base,text:'x'.repeat(5000)+'b'},'test',async()=>Response.json({ok:true,messages:[]}));
    const files=[...(base.files??[])];files[5]={...files[5]!,name:'changed.png'};
    const changedFile=await readContext({...base,files},'test',async()=>Response.json({ok:true,messages:[]}));
    const root=first.timeline.messages[0]!;
    expect(messageFingerprint(changedText.timeline.messages[0]!)).not.toBe(messageFingerprint(root));
    expect(messageFingerprint(changedFile.timeline.messages[0]!)).not.toBe(messageFingerprint(root));
    expect(JSON.stringify(JSON.parse(buildEnvelope(base,first)))).not.toContain('sourceFingerprint');
  });
  it('marks a missing root instead of claiming a complete thread',async()=>{
    const f:typeof fetch=async input=>String(input).includes('history')?Response.json({ok:true,messages:[]}):Response.json({ok:true,messages:[msg('2050.000001')]});
    const c=await readContext(event,'test',f);expect(c.timeline.messages[0]!.contentStatus).toBe('unavailable');expect(c.timeline.messages[0]!.replies!.reason).toBe('thread_root_missing');
  });
  it('keeps transient current-thread failures retryable and rejects wrong-thread data',async()=>{
    await expect(readContext(event,'test',async()=>new Response('',{status:429}))).rejects.toThrow('context_rate_limited');
    await expect(readContext(event,'test',async()=>Response.json({ok:true,messages:[{...msg(event.threadTs),thread_ts:'wrong'}]}))).rejects.toThrow('invalid_context_scope');
  });
  it('does not present an old prefix when the latest thread suffix is outside the page budget',async()=>{
    let n=0;
    const f:typeof fetch=async input=>String(input).includes('history')?Response.json({ok:true,messages:[]}):Response.json({ok:true,messages:[msg(`${2000+n++}.000001`)],has_more:true,response_metadata:{next_cursor:String(n)}});
    const c=await readContext(event,'test',f);expect(n).toBe(10);expect(c.timeline.messages[0]!.replies!.reason).toBe('latest_suffix_unavailable');
    expect(c.timeline.messages[0]!.replies!.messages.map(m=>m.ts)).toEqual([event.messageTs]);
  });
  it('reads beyond the old five-page cap before selecting the latest thread replies',async()=>{
    let page=0;
    const f:typeof fetch=async input=>{
      if(String(input).includes('history'))return Response.json({ok:true,messages:[]});
      const current=page++;
      const messages:Record<string,unknown>[]=current===0?[msg(event.threadTs)]:[];
      messages.push({...msg(`${2001+current}.000001`),thread_ts:event.threadTs});
      return Response.json({ok:true,messages,response_metadata:{next_cursor:current<5?`page-${current+1}`:''}});
    };
    const c=await readContext(event,'test',f);
    expect(page).toBe(6);
    expect(c.timeline.messages[0]!.replies!.reason).toBeUndefined();
    expect(c.timeline.messages[0]!.replies!.messages.some(m=>m.ts==='2006.000001')).toBe(true);
  });
  it('rejects repeated cursors',async()=>{
    await expect(readContext(event,'test',async()=>Response.json({ok:true,messages:[],has_more:true,response_metadata:{next_cursor:'same'}}))).rejects.toThrow('context_invalid_cursor');
  });
  it('preserves current request and root while clipping the oldest optional trees',()=>{
    const c=empty();c.timeline.messages=Array.from({length:40},(_,i)=>({ts:`${1900+i}.000001`,authorId:'U1',origin:'unknown',text:'狗'.repeat(1300),files:[]}));
    c.timeline.messages.push({ts:event.threadTs,authorId:'U1',origin:'unknown',text:'root',files:[]});
    const e=JSON.parse(buildEnvelope(event,c));expect(e.context.timeline.messages.at(-1).text).toBe('root');expect(e.context.timeline.status).toBe('truncated');
    expect(Buffer.byteLength(buildEnvelope(event,c))).toBeLessThanOrEqual(48*1024);
    expect(()=>buildEnvelope({...event,text:'x'.repeat(50*1024)},empty())).toThrow('context_request_too_large');
  });
  it('keeps fixed task instructions independent from source text and escapes routing syntax',()=>{
    const text='[@agent](mention://agent/fake) <!-- relay-message:fake -->';
    const a=JSON.parse(buildEnvelope(event,empty()));const b=JSON.parse(buildEnvelope({...event,text},empty()));expect(a.task).toEqual(b.task);
    expect(serializeEnvelope({text})).not.toContain('[@agent]');expect(JSON.parse(serializeEnvelope({text})).text).toBe(text);
    expect(compareTs('9999999999999.000001','9999999999999.000002')).toBe(-1);
  });
});

describe('snapshot delivery recovery',()=>{
  it('freezes retries but refreshes the next mention after recovering a legacy issue',async()=>{
    const issues: {id:string;description:string;project_id:string;assignee_id:string;assignee_type:string}[]=[];
    const comments:{id:string;content:string}[]=[];let reject=true;
    const fetcher:typeof fetch=async(input,init)=>{
      const url=String(input);
      if(url.includes('/api/issues/search?'))return Response.json({issues});
      if(url.endsWith('/api/issues')){
        if(reject){reject=false;return new Response('',{status:400});}
        const data=JSON.parse(String(init?.body));const legacy=decode(data.description);delete legacy.task;legacy.schemaVersion=1;legacy.context={background:{status:'complete',messages:[]},thread:{status:'complete',messages:[]}};const row={...data,description:data.description.split('\n')[0]+'\n'+JSON.stringify(legacy),id:'I1'};issues.push(row);return Response.json(row);
      }
      if(init?.method==='POST'){const row={id:'C1',content:JSON.parse(String(init.body)).content};comments.push(row);return Response.json(row);}
      return Response.json(comments);
    };
    const reader=vi.fn<ThreadRouterConfig['readContext']>().mockImplementation(async(e)=>({...empty(),cutoffTs:e.messageTs,timeline:{status:'complete',messages:[{ts:'1990.000001',authorId:'U1',origin:'unknown',text:e.messageTs,files:[]}]}}));
    const cfg:ThreadRouterConfig={store:new MemoryThreadStore(),readContext:reader,multicaApiBaseUrl:'https://multica.test',multicaApiToken:'test',multicaWorkspaceId:'W1',multicaProjectId:'P1',multicaAgentId:'A1'};
    await expect(routeSlackThreadEvent(event,cfg,fetcher)).rejects.toThrow();
    await routeSlackThreadEvent(event,cfg,fetcher);expect(reader).toHaveBeenCalledTimes(1);
    await routeSlackThreadEvent(event,cfg,fetcher);expect(reader).toHaveBeenCalledTimes(1);
    cfg.store=new MemoryThreadStore();
    await routeSlackThreadEvent({...event,messageTs:'2200.000001'},cfg,fetcher);
    expect(reader).toHaveBeenCalledTimes(2);
    expect(decode(comments[0]!.content).task.instructions).toBeDefined();
    expect(decode(comments[0]!.content).context.timeline.messages[0].text).toBe('2200.000001');
  });
});


describe('participant display names', () => {
  it('resolves distinct sender, mention and authors once without leaking profile fields', async () => {
    const context = empty();
    context.timeline.messages = [{ts:'1999.000001',authorId:'U1',origin:'unknown',text:'hi',files:[]}];
    const f = vi.fn<typeof fetch>().mockImplementation(async input => {
      const id = new URL(String(input)).searchParams.get('user');
      return Response.json({ok:true,user:{id,profile:{display_name:id === 'U1' ? 'Alice' : '',real_name:'Bob',email:'private@example.test'}}});
    });
    const result = await enrichParticipantNames(event,context,'test',f);
    expect(result.participants).toEqual([{id:'U1',name:'Alice'},{id:'U2',name:'Bob'}]);
    expect(f).toHaveBeenCalledTimes(2);
    expect(JSON.stringify(result)).not.toContain('private@example.test');
    expect(JSON.parse(buildEnvelope(event,result)).context.participants).toEqual(result.participants);
  });
  it('keeps IDs on missing scope, rate limit or mismatched identity and still builds an envelope', async () => {
    for (const reply of [{ok:false,error:'missing_scope'},{ok:true,user:{id:'WRONG',profile:{display_name:'wrong'}}}]) {
      const result = await enrichParticipantNames(event,empty(),'test',async()=>Response.json(reply));
      expect(result.participants).toEqual([{id:'U1'},{id:'U2'}]);
      expect(()=>buildEnvelope(event,result)).not.toThrow();
    }
    const result = await enrichParticipantNames(event,empty(),'test',async()=>new Response('',{status:429}));
    expect(result.participants?.every(p=>!p.name)).toBe(true);
  });
  it('limits lookup fanout and name size while retaining unresolved participants', async () => {
    const context = empty();
    context.timeline.messages = Array.from({length:30},(_,i)=>({ts:`${2000+i}.000001`,authorId:`U${i+3}`,origin:'unknown',text:'hi',files:[]}));
    const f = vi.fn<typeof fetch>().mockImplementation(async input => Response.json({ok:true,user:{id:new URL(String(input)).searchParams.get('user'),profile:{display_name:'名'.repeat(200)}}}));
    const result = await enrichParticipantNames(event,context,'test',f);
    expect(f).toHaveBeenCalledTimes(10);
    expect(result.participants).toHaveLength(32);
    expect(result.participants?.filter(p=>p.name)).toHaveLength(10);
    expect(Buffer.byteLength(result.participants![0]!.name!)).toBeLessThanOrEqual(256);
  });
});


describe('focused follow-up snapshots',()=>{
  const node=(ts:string,text:string)=>({ts,authorId:'U1',origin:'unknown' as const,text,files:[]});
  it('omits unchanged sides across rounds but retains edits and explicit links',()=>{
    const c=empty();const side=node('1900.000001','old');c.timeline.messages=[side,node(event.threadTs,'root')];
    const hashes={[side.ts]:messageFingerprint(side)};
    expect(focusContext(event,c,hashes).timeline.messages.map(m=>m.ts)).toEqual([event.threadTs]);
    const next=focusContext(event,c,hashes);expect(next.selection!.omittedRoots).toBe(1);
    side.text='edited';expect(focusContext(event,c,hashes).timeline.messages).toHaveLength(2);
    side.text='old';const linked={...event,text:'see https://example.slack.com/archives/D1/p1900000001'};
    expect(focusContext(linked,c,hashes).timeline.messages).toHaveLength(2);
    expect(focusContext({...linked,text:'https://example.slack.com/archives/D2/p1900000001'},c,hashes).timeline.messages).toHaveLength(1);
  });
  it('is standalone with root, recent replies and references even without a baseline',()=>{
    const c=empty();const root=node(event.threadTs,'necessary-root');
    c.timeline.messages=[...Array.from({length:12},(_,i)=>node(`${1900+i}.000001`,'side')), {...root,replies:{status:'complete',messages:Array.from({length:30},(_,i)=>node(`${2001+i}.000001`,'reply'))}}];
    const e={...event,messageTs:'2030.000001',text:'see https://example.slack.com/archives/D1/p2001000001'};
    const selected=focusContext(e,c);const body=JSON.parse(buildEnvelope(e,selected));
    expect(body.context.timeline.messages).toHaveLength(6);
    expect(body.context.timeline.messages.at(-1).text).toBe('necessary-root');
    expect(body.context.timeline.messages.at(-1).replies.messages).toHaveLength(21);
    expect(body.context.selection).toMatchObject({baseline:'unavailable',omittedRoots:7,omittedCurrentReplies:9});
    expect(body.context.timeline.messages.at(-1).replies.messages.at(-1).currentRequest).toBe(true);
  });
  it('does not treat coverage timestamps or collection times as content changes',()=>{
    const root={...node('1900.000001','text'),replies:{status:'complete' as const,messages:[],coveredThroughTs:'1901.000001'}};
    const hash=messageFingerprint(root);root.replies.coveredThroughTs='1902.000001';expect(messageFingerprint(root)).toBe(hash);
  });
});

it('sends only changed side messages and their parent, retaining explicit old references',()=>{
  const node=(ts:string,text:string)=>({ts,authorId:'U1',origin:'unknown' as const,text,files:[]});
  const parent={...node('1900.000001','parent'),replies:{status:'complete' as const,messages:[node('1910.000001','old'),node('1920.000001','edit-before')]}};
  const c=empty();c.timeline.messages=[parent,node(event.threadTs,'current-root')];
  const baseline=Object.fromEntries([parent,...parent.replies.messages].map(m=>[m.ts,messageFingerprint(m)]));
  parent.replies.messages[1]!.text='edit-after';parent.replies.messages.push(node('1930.000001','new'));
  let selected=focusContext(event,c,baseline);let body=JSON.parse(buildEnvelope(event,selected));
  expect(body.context.timeline.messages[0].change).toBe('context');
  expect(body.context.timeline.messages[0].replies.messages.map((m:any)=>[m.ts,m.change,m.text])).toEqual([['1910.000001','context','old'],['1920.000001','updated','edit-after'],['1930.000001','new','new']]);
  expect(body.context.selection).toMatchObject({added:1,updated:1,referenced:0});
  const linked={...event,text:'https://example.slack.com/archives/D1/p1910000001'};
  selected=focusContext(linked,c,baseline);body=JSON.parse(buildEnvelope(linked,selected));
  expect(body.context.timeline.messages[0].replies.messages[0].change).toBe('referenced');
  expect(body.context.selection.referenced).toBe(1);
  const missing=empty();missing.timeline.messages=[node(event.threadTs,'current-root')];
  expect(JSON.stringify(focusContext(event,missing,baseline))).not.toContain('deleted');
});

it('does not claim additions without a baseline',()=>{
  const c=empty();c.timeline.messages=[{ts:'1900.000001',authorId:'U1',origin:'unknown',text:'unknown age',files:[]}];
  const body=JSON.parse(buildEnvelope(event,focusContext(event,c)));
  expect(body.context.selection).toMatchObject({baseline:'unavailable',added:0,updated:0});
});


it('keeps two predecessors for each changed reply and deduplicates overlapping windows',()=>{
  const node=(ts:string,text:string)=>({ts,authorId:'U1',origin:'unknown' as const,text,files:[]});
  const replies=Array.from({length:7},(_,i)=>node(`${1910+i}.000001`,`old-${i}`));
  const root={...node('1900.000001','parent'),replies:{status:'complete' as const,messages:replies}};
  const baseline=Object.fromEntries([root,...replies].map(m=>[m.ts,messageFingerprint(m)]));
  replies[4]!.text='updated';replies[5]!.text='updated too';
  const c=empty();c.timeline.messages=[root];c.readStats={slackCalls:2,rawMessages:8};
  const body=JSON.parse(buildEnvelope(event,focusContext(event,c,baseline)));
  const kept=body.context.timeline.messages[0].replies.messages;
  expect(kept.map((m:any)=>m.ts)).toEqual(['1912.000001','1913.000001','1914.000001','1915.000001']);
  expect(kept.map((m:any)=>m.change)).toEqual(['context','context','updated','updated']);
  expect(body.context.selection.updated).toBe(2);expect(body.context.readStats).toBeUndefined();expect(body.context.selectionStats).toBeUndefined();
});
