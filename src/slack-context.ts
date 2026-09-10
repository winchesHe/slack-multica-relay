import { Buffer } from 'node:buffer';
import { clip, compareTs, markTruncated, projectFiles, sourceMessageFingerprint, timestampValid, updateCoverage, type ContextSection, type ContextMessage, type ThreadContext } from './context-envelope.js';
import type { SlackThreadEvent } from './thread-router.js';

type RawMessage = Record<string, unknown>;
const permissionErrors = new Set(['missing_scope', 'not_in_channel', 'channel_not_found', 'access_denied', 'token_revoked', 'invalid_auth', 'account_inactive', 'thread_not_found']);
const MAX_CONTEXT_CALLS = 20;
const MAX_OPTIONAL_ROOTS = 5;
const MAX_NAME_LOOKUPS = 10;
const project = (raw: RawMessage): ContextMessage => {
  const text = typeof raw.text === 'string' ? raw.text : '';
  return { ts: String(raw.ts), authorId: typeof raw.user === 'string' ? raw.user : typeof raw.bot_id === 'string' ? raw.bot_id : '',
    origin: raw.bot_id || raw.app_id ? 'bot_or_app' : 'unknown', text: clip(text,4096), files: projectFiles(raw.files),
    sourceFingerprint: typeof raw.sourceFingerprint === 'string' && /^[a-f0-9]{64}$/u.test(raw.sourceFingerprint)
      ? raw.sourceFingerprint : sourceMessageFingerprint(raw),
    ...(Buffer.byteLength(text)>4096 ? {textTruncated:true} : {}),
    ...(Array.isArray(raw.files)&&raw.files.length>5 ? {filesTruncated:true} : {}) };
};
export async function readContext(event: SlackThreadEvent, token: string, fetchImpl: typeof fetch = fetch): Promise<ThreadContext> {
  const readStartedAt = Date.now();
  if (!timestampValid(event.threadTs) || !timestampValid(event.messageTs) || compareTs(event.threadTs,event.messageTs)>0) throw new Error('invalid_context_scope');
  const deadline = AbortSignal.timeout(20_000);
  const [seconds,fraction] = event.messageTs.split('.');
  const oldest = `${BigInt(seconds!)>86_400n ? BigInt(seconds!)-86_400n : 0n}.${fraction}`;
  const threadRoots = new Map<string,string>();
  let requests = 0, rawMessages = 0, sideStopped = false;
  const read = async (method: 'history'|'replies', rootTs = event.threadTs, optional = false): Promise<ContextSection> => {
    const section: ContextSection = {status:'complete',messages:[]};
    let cursor = ''; const cursors = new Set<string>();
    try {
      const pageLimit = method === 'history' ? 5 : optional ? 3 : 10;
      let reachedEnd = false;
      for (let page=0;page<pageLimit;page++) {
        if (requests>=MAX_CONTEXT_CALLS || (optional && (sideStopped || deadline.aborted))) { markTruncated(section,'read_budget'); break; }
        deadline.throwIfAborted(); requests++;
        const params = new URLSearchParams({channel:event.channelId,limit:optional?'100':'200',latest:event.messageTs,inclusive:'true',
          ...(method==='history'?{oldest}:{ts:rootTs}),...(cursor?{cursor}:{})});
        const response = await fetchImpl(`https://slack.com/api/conversations.${method}?${params}`,{headers:{authorization:`Bearer ${token}`},signal:deadline});
        if (response.status===429) throw new Error('context_rate_limited');
        if (!response.ok) throw new Error('context_upstream_failed');
        const reader=response.body?.getReader(); if(!reader)throw new Error('context_invalid_response');
        const chunks:Uint8Array[]=[];let bytes=0;
        try { for (;;) {const part=await reader.read();if(part.done)break;bytes+=part.value.byteLength;if(bytes>2*1024*1024)throw new Error('context_response_too_large');chunks.push(part.value);} }
        finally {await reader.cancel().catch(()=>{});}
        const body=JSON.parse(Buffer.concat(chunks).toString('utf8'));
        if(body.ok!==true) {if(permissionErrors.has(body.error))return {status:'unavailable',reason:body.error,messages:[]};throw new Error('context_upstream_failed');}
        if(!Array.isArray(body.messages))throw new Error('context_invalid_response');
        rawMessages += body.messages.length;
        for(const raw of body.messages as RawMessage[]) {
          if(!timestampValid(raw.ts))throw new Error('context_invalid_response');
          if(raw.channel&&raw.channel!==event.channelId)throw new Error('invalid_context_scope');
          if(method==='replies'&&raw.thread_ts&&raw.thread_ts!==rootTs)throw new Error('invalid_context_scope');
          if(compareTs(raw.ts,event.messageTs)>0)continue;
          if(method==='history'&&(compareTs(raw.ts,oldest)<0||(raw.thread_ts&&raw.thread_ts!==raw.ts)))continue;
          if(method==='replies'&&compareTs(raw.ts,rootTs)<0)continue;
          if(raw.subtype&&!['bot_message','file_share','thread_broadcast'].includes(String(raw.subtype)))continue;
          if(method==='history'&&typeof raw.reply_count==='number'&&raw.reply_count>0)
            threadRoots.set(raw.ts,timestampValid(raw.latest_reply)&&compareTs(raw.latest_reply,event.messageTs)<=0?raw.latest_reply:raw.ts);
          if(!section.messages.some(m=>m.ts===raw.ts))section.messages.push(project(raw));
        }
        cursor=body.response_metadata?.next_cursor?.trim()||'';
        if(!cursor&&body.has_more===true)throw new Error('context_invalid_cursor');
        if(!cursor){reachedEnd=true;break;}
        if(cursors.has(cursor))throw new Error('context_invalid_cursor');cursors.add(cursor);
        if(method==='history'&&section.messages.length>=40){markTruncated(section,'timeline_root_limit');break;}
        if(page===pageLimit-1)markTruncated(section,'page_limit');
      }
      if(method==='replies'&&!reachedEnd){
        section.messages=section.messages.filter(m=>m.ts===rootTs);
        markTruncated(section,'latest_suffix_unavailable');
      }
    } catch(error) {
      if(!optional || (error instanceof Error && error.message==='invalid_context_scope'))throw error;
      sideStopped=true;
      if(method==='replies')section.messages=section.messages.filter(m=>m.ts===rootTs);
      markTruncated(section,error instanceof Error&&error.message==='context_rate_limited'?'rate_limited':'read_failed');
    }
    if (method==='replies' && section.status==='complete' && !section.messages.some(m=>m.ts===rootTs)) return {status:'unavailable',reason:'thread_root_missing',messages:[]};
    section.messages.sort((a,b)=>compareTs(a.ts,b.ts));
    const limit=method==='history'?40:optional?21:101;
    if(section.messages.length>limit){
      const root=method==='replies'?section.messages.find(m=>m.ts===rootTs):undefined;
      section.messages=[...(root?[root]:[]),...section.messages.filter(m=>!root||m.ts!==root.ts).slice(-(limit-(root?1:0)))];
      markTruncated(section,method==='history'?'timeline_root_limit':'thread_message_limit');
    }
    if(section.messages.some(m=>m.textTruncated||m.filesTruncated))markTruncated(section,section.reason||'message_projection_limit');
    updateCoverage(section); return section;
  };
  // Reserve the first reads for the current thread before expanding optional branches.
  const current = event.messageTs===event.threadTs ? {status:'complete' as const,messages:[project({ts:event.messageTs,user:event.senderUserId,text:event.text,files:event.files,sourceFingerprint:event.sourceFingerprint})]} : await read('replies');
  let currentRoot=current.messages.find(m=>m.ts===event.threadTs);
  if(!currentRoot)currentRoot={ts:event.threadTs,authorId:'',origin:'unknown',text:'',files:[],contentStatus:'unavailable'};
  const currentReplies:ContextSection={...current,messages:current.messages.filter(m=>m.ts!==event.threadTs)};
  if(event.messageTs!==event.threadTs&&!currentReplies.messages.some(m=>m.ts===event.messageTs))currentReplies.messages.push(project({ts:event.messageTs,user:event.senderUserId,text:event.text,files:event.files,sourceFingerprint:event.sourceFingerprint}));
  currentReplies.messages.sort((a,b)=>compareTs(a.ts,b.ts));
  if(currentReplies.messages.length>100){currentReplies.messages=currentReplies.messages.slice(-100);markTruncated(currentReplies,'thread_message_limit');}
  updateCoverage(currentReplies);currentRoot.replies=currentReplies;
  const timeline=await read('history');
  timeline.messages=timeline.messages.filter(m=>m.ts!==event.threadTs);
  const optionalRoots=timeline.messages.filter(m=>threadRoots.has(m.ts))
    .sort((a,b)=>compareTs(threadRoots.get(b.ts)!,threadRoots.get(a.ts)!)).slice(0,MAX_OPTIONAL_ROOTS);
  let next=0;
  await Promise.all(Array.from({length:Math.min(4,optionalRoots.length)},async()=>{
    while(next<optionalRoots.length){const root=optionalRoots[next++]!;const section=await read('replies',root.ts,true);section.messages=section.messages.filter(m=>m.ts!==root.ts);updateCoverage(section);root.replies=section;}
  }));
  // Current-thread replies have priority; nearest optional branches consume the remaining total budget.
  let remaining=Math.max(0,200-currentReplies.messages.length);
  for(const root of optionalRoots){const replies=root.replies!;if(replies.messages.length>remaining){replies.messages=remaining?replies.messages.slice(-remaining):[];markTruncated(replies,'total_reply_limit');updateCoverage(replies);}remaining-=replies.messages.length;}
  timeline.messages.push(currentRoot);timeline.messages.sort((a,b)=>compareTs(a.ts,b.ts));updateCoverage(timeline);
  return {anchorTs:event.threadTs,cutoffTs:event.messageTs,capturedAt:new Date().toISOString(),timeline,
    readStats:{slackCalls:requests,rawMessages,messageReadMs:Date.now()-readStartedAt}};
}

// Names are display metadata only. Lookup failures must not block delivery of the request.
export async function enrichParticipantNames(event: SlackThreadEvent, context: ThreadContext, token: string, fetchImpl: typeof fetch = fetch): Promise<ThreadContext> {
  const nameStartedAt = Date.now();
  const ids = [...new Set([event.senderUserId, ...(event.mention.type === 'user' ? [event.mention.id] : []),
    ...context.timeline.messages.flatMap(m => [m.authorId, ...(m.replies?.messages.map(r => r.authorId) ?? [])])])].filter(Boolean);
  const participants: NonNullable<ThreadContext['participants']> = ids.map(id => ({ id }));
  const pending = participants.filter(p => /^[UW][A-Z0-9]+$/.test(p.id)).slice(0, MAX_NAME_LOOKUPS);
  const deadline = AbortSignal.timeout(3_000);
  let next = 0, stopped = false, lookups = 0;
  await Promise.all(Array.from({ length: Math.min(4, pending.length) }, async () => {
    while (!stopped && !deadline.aborted && next < pending.length) {
      const person = pending[next++]!;
      try {
        lookups++;
        const response = await fetchImpl(`https://slack.com/api/users.info?${new URLSearchParams({ user: person.id })}`, {
          headers: { authorization: `Bearer ${token}` }, signal: deadline,
        });
        if (!response.ok) { stopped = true; continue; }
        const reader = response.body?.getReader();
        if (!reader) continue;
        const chunks: Uint8Array[] = []; let bytes = 0;
        try {
          for (;;) {
            const part = await reader.read(); if (part.done) break;
            bytes += part.value.byteLength;
            if (bytes > 128 * 1024) throw new Error('profile_response_too_large');
            chunks.push(part.value);
          }
        } finally { await reader.cancel().catch(() => {}); }
        const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        if (!body.ok) { if (body.error !== 'user_not_found') stopped = true; continue; }
        if (body.user?.id !== person.id) continue;
        const profile = body.user.profile;
        const name = [profile?.display_name, profile?.real_name, body.user.name]
          .find(value => typeof value === 'string' && value.trim());
        if (name) person.name = clip(name.trim(), 256);
      } catch { stopped = true; }
    }
  }));
  return { ...context, participants, readStats: {
    ...(context.readStats ?? {slackCalls:0,rawMessages:0}), nameLookupCalls:lookups, nameReadMs:Date.now()-nameStartedAt,
  } };
}
