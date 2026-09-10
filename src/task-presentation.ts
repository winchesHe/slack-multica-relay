import { Buffer } from 'node:buffer';
import {createHash} from 'node:crypto';
import type {SlackThreadEvent} from './thread-router.js';
import {clip,serializeEnvelope} from './context-envelope.js';
const PAYLOAD_START='<!-- relay-payload:v1 -->';
const PAYLOAD_END='<!-- /relay-payload -->';
function object(value:unknown):value is Record<string,unknown>{return !!value&&typeof value==='object'&&!Array.isArray(value);}
function decodeSlack(text: string): string {
  return text
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

export function formatTaskTitle(
  event: SlackThreadEvent,
  scope: string,
): string {
  const pr = event.text.match(
    /https:\/\/github\.com\/[^/\s<>|]+\/([^/\s<>|]+)\/pull\/(\d+)(?=[/?#\s<>|]|$)/,
  );
  const summary = decodeSlack(
    event.text
      .replace(/<@[^>]+>|<!subteam\^[^>]+>/g, "")
      .replace(/<https?:\/\/[^>]+>|https?:\/\/\S+/g, ""),
  )
    .replace(/^\s*cc\b\s*/i, "")
    .replace(/\s+/g, " ")
    .trim();
  const label = summary || (pr ? `${pr[1]} #${pr[2]}` : "自动任务");
  // 同一正文的不同线程或 Agent scope 仍须有不同标题，避免服务端同标题去重。
  const identity = createHash("sha256")
    .update(`${scope}:${event.teamId}:${event.channelId}:${event.threadTs}`)
    .digest("hex")
    .slice(0, 16);
  const chars = Array.from(label);
  return `Slack mention · ${chars.slice(0, 80).join("")}${chars.length > 80 ? "..." : ""} · [${identity}]`;
}

function quoteMessage(text: string): string {
  // 原文只作为引用展示，不让消息里的 HTML、围栏或链接语法改写描述结构。
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/[\\`*_[\]{}()!#|~]/g, "\\$&")
    .split(/\r?\n/)
    .map((line) => `> ${line}`)
    .join("\n");
}


export function formatTaskDescription(envelope:string, marker:string, followup=false):string {
  const payload=JSON.parse(envelope);const event=payload.eventPayload as SlackThreadEvent;
  const raw=clip(event.text,4096);const quote=quoteMessage(decodeSlack(raw))+(raw!==event.text?'\n> （展示已截断，完整请求见数据区）':'');
  const date=new Date(Number(event.messageTs)*1000);
  const time=Number.isFinite(date.getTime())?new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(date)+'（Asia/Shanghai）':'未知';
  const link='https://slack.com/app_redirect?'+new URLSearchParams({team:event.teamId,channel:event.channelId});
  const selection=payload.context?.selection;
  const summary=selection?.mode==='focused' && selection.added!==undefined ? `旁支变化：新增 ${selection.added} 条，更新 ${selection.updated} 条，明确引用 ${selection.referenced} 条。` : '';
  const render=(json:string)=>{
    const fence='`'.repeat((json.match(/`+/g)??[]).reduce((n,run)=>Math.max(n,run.length+1),3));
    const fileCount=Array.isArray(event.files)?event.files.length:0;
    return [marker,followup?'## Slack thread 后续消息':'## Slack 原始消息',quote,...(summary?[summary]:[]),'## 来源',`- Slack 频道：[打开频道](${link})`,`- 触发时间：${time}`,`- 附件：${fileCount}${event.filesTruncated?'+':''} 个${event.filesTruncated?'（引用已截断）':''}`,'## 事件上下文',PAYLOAD_START,`${fence}json\n${json}\n${fence}`,PAYLOAD_END].join('\n\n').replace(marker+'\n\n',marker+'\n');
  };
  let body=render(serializeEnvelope(payload,2));
  if(Buffer.byteLength(body)>64*1024)body=render(envelope);
  if(Buffer.byteLength(body)>64*1024)throw new Error('task_presentation_too_large');
  return body;
}
export interface RecoveredMessage {
  teamId: string;
  channelId: string;
  threadTs: string;
  messageTs: string;
}

export function readTaskEnvelope(description: string): { eventPayload: SlackThreadEvent; [key:string]: unknown } {
  const body = description
    .slice(description.indexOf("\n") + 1)
    .replace(/\r\n/g, "\n");
  let json = body;
  const lines = body.split("\n");
  const starts = lines.filter((line) => line === PAYLOAD_START);
  const ends = lines.filter((line) => line === PAYLOAD_END);
  if (starts.length || ends.length) {
    if (starts.length !== 1 || ends.length !== 1)
      throw new Error("invalid_thread_state");
    const start = lines.indexOf(PAYLOAD_START);
    const end = lines.indexOf(PAYLOAD_END);
    const block = lines
      .slice(start + 1, end)
      .join("\n")
      .trim();
    const match = block.match(/^(`{3,})json\n([\s\S]*)\n\1$/);
    if (end < start || !match) throw new Error("invalid_thread_state");
    json = match[2];
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(json);
  } catch {
    throw new Error("invalid_thread_state");
  }
  const event = object(parsed) ? parsed.eventPayload : undefined;
  if (
    !object(event) ||
    !["teamId", "channelId", "threadTs", "messageTs"].every(
      (key) => typeof event[key] === "string" && event[key].length > 0,
    ) ||
    !/^\d+\.\d+$/.test(event.threadTs as string) ||
    !/^\d+\.\d+$/.test(event.messageTs as string)
  )
    throw new Error("invalid_thread_state");
  return parsed as {eventPayload:SlackThreadEvent;[key:string]:unknown};
}
