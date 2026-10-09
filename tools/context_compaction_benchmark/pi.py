"""Execute Pi's real cut, summary request planning and file tracking.

Only ordinary messages are supported. Network calls are captured as request plans
and executed by the common loopback transport; Pi session I/O is not loaded.
"""
import json
import subprocess
from pathlib import Path

NODE_REPLAY = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import {stripTypeScriptTypes} from 'node:module';
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const clean = source => source.replace(/^import[\s\S]*?;\r?$/gm,'').replace(/^export /gm,'');
const read = path => fs.readFileSync(input.root+'/'+path,'utf8').replaceAll('\r\n','\n');
const text=vm.runInNewContext(stripTypeScriptTypes(clean(read('packages/ai/src/utils/text.ts'))) + ';({contentText})');
const messages=vm.runInNewContext(stripTypeScriptTypes(clean(read('packages/coding-agent/src/core/messages.ts'))) + ';({convertToLlm})');
const usage=vm.runInNewContext(stripTypeScriptTypes(clean(read('packages/coding-agent/src/core/usage-totals.ts'))) + ';({combineUsage})');
const utils=vm.runInNewContext(stripTypeScriptTypes(clean(read('packages/coding-agent/src/core/compaction/utils.ts'))) +
  ';({createFileOps,extractFileOpsFromMessage,computeFileLists,formatFileOperations,serializeConversation,SUMMARIZATION_SYSTEM_PROMPT})',text);
const fullManager=read('packages/coding-agent/src/core/session-manager.ts');
const begin=fullManager.indexOf('export function sessionEntryToContextMessages(');
if(begin<0) throw new Error('Pi session entry source boundary changed');
const end=fullManager.indexOf('\n/**',begin);
const project=vm.runInNewContext(stripTypeScriptTypes(clean(fullManager.slice(begin,end<0?undefined:end))) + ';({sessionEntryToContextMessages})');
const plans=[];
const api=vm.runInNewContext(stripTypeScriptTypes(clean(read('packages/coding-agent/src/core/compaction/compaction.ts'))) +
  ';({findProjectedCutPoint,estimateContextTokens,prepareCompaction,compact})', {...text,...messages,...utils,...project,...usage,
  buildSessionProjection: entries=>({entries:entries.map(sourceEntry=>({sourceEntry,messages:project.sessionEntryToContextMessages(sourceEntry)})),
    messages:entries.flatMap(project.sessionEntryToContextMessages)}),
  getCurrentSystemMessage: messages=>messages.findLast(message=>message.role==='system'),
  normalizeContext: value=>value,uuidv7:()=> 'replay-routing',retryAssistantCall:produce=>produce(),
  completeSimple:async (model,context,options)=> {plans.push({...context,maxTokens:options.maxTokens});
    return {role:'assistant',content:[{type:'text',text:'REFERENCE_ANSWER_'+(plans.length-1)}],stopReason:'stop',
      usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,
             cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};}});
const entries=input.history.map((message,index)=> ({id:'entry-'+index,type:'message',message}));
const projectedEntries=entries.map(sourceEntry=>({sourceEntry,messages:project.sessionEntryToContextMessages(sourceEntry)}));
const cut=api.findProjectedCutPoint(projectedEntries,0,entries.length,input.keep_tokens);
const preparation=api.prepareCompaction(entries,{reserveTokens:input.reserve_tokens,keepRecentTokens:input.keep_tokens});
if(!preparation) process.stdout.write(JSON.stringify({available:false,cut:cut.firstKeptEntryIndex,plans:[],summary:'',split:cut.isSplitTurn}));
else {
  preparation.previousSummary=input.previous_summary;
  const result=await api.compact(preparation,{maxTokens:input.reserve_tokens},undefined);
  const projected=messages.convertToLlm([{role:'compactionSummary',summary:result.summary,tokensBefore:result.tokensBefore,timestamp:0}]);
  process.stdout.write(JSON.stringify({available:true,cut:cut.firstKeptEntryIndex,split:cut.isSplitTurn,plans,summary:result.summary,
    checkpoint:projected[0].content[0].text,details:result.details}));
}
"""


def select_pi(root, history, keep_tokens=2000, reserve_tokens=1024, previous_summary=None):
    converted = []
    for item in history:
        role = item.get("role")
        if role == "assistant":
            content = [{"type": "text", "text": item.get("content", "")}]
            content += [{"type": "toolCall", "id": call["id"], "name": call["function"]["name"],
                         "arguments": json.loads(call["function"]["arguments"])} for call in item.get("tool_calls", [])]
            converted.append({"role": role, "content": content})
        elif role == "tool":
            converted.append({"role": "toolResult", "toolCallId": item["tool_call_id"],
                              "content": [{"type": "text", "text": item.get("content", "")}]})
        elif role in {"user", "system"}:
            converted.append(item)
        else:
            raise ValueError("Pi replay only supports ordinary Chat protocol messages")
    config = {"root": str(Path(root).resolve()), "history": converted, "keep_tokens": keep_tokens,
              "reserve_tokens": reserve_tokens, "previous_summary": previous_summary}
    process = subprocess.run(["node", "--input-type=module", "-e", NODE_REPLAY], input=json.dumps(config),
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
    if process.returncode:
        raise ValueError(f"Pi reference replay failed: {process.stderr[-1500:]}")
    return json.loads(process.stdout)


def attach_reference_policy(provider, root):
    from dataclasses import replace

    original_send = provider.send
    previous_summary, checkpoint_text = None, ""
    provider._compaction.compact = lambda *args, **kwargs: None
    provider._schedule_maintenance = lambda: None

    def send(text, timeout=None):
        nonlocal previous_summary, checkpoint_text
        import time
        deadline = time.monotonic() + (timeout or provider.timeout)
        prepared = provider._codec.prepare(provider._messages, [{"role": "user", "content": text}],
                                           system=provider.system_prompt, tools=None)
        measured = provider._count_context(prepared, None, deadline)
        if measured.value is not None and measured.value > provider.context_budget.input_limit:
            history = [item for item in provider._messages if not checkpoint_text or item.get("content") != checkpoint_text]
            selected = select_pi(root, history, provider.context_budget.keep_recent_tokens,
                                 provider.context_budget.output_tokens, previous_summary)
            if not selected["available"]:
                from codey.providers.error_classification import ContextOverflowError
                raise ContextOverflowError("Pi reference has no closed source range that reduces this request")
            summary, checkpoint_text = selected["summary"], selected["checkpoint"]
            for index, plan in enumerate(selected["plans"]):
                auxiliary = provider.fork_auxiliary()
                auxiliary.system_prompt = plan["systemPrompt"]
                reserve = auxiliary.context_budget.output_tokens + auxiliary.context_budget.safety_tokens
                auxiliary._context_budget = replace(auxiliary.context_budget, output_tokens=plan["maxTokens"],
                                                    safety_tokens=reserve - plan["maxTokens"])
                try:
                    answer = auxiliary.send(plan["messages"][0]["content"][0]["text"],
                                            timeout=max(0.001, deadline - time.monotonic()))
                finally:
                    auxiliary.close()
                summary = summary.replace(f"REFERENCE_ANSWER_{index}", answer)
                checkpoint_text = checkpoint_text.replace(f"REFERENCE_ANSWER_{index}", answer)
            previous_summary = summary
            provider._messages = [{"role": "user", "content": checkpoint_text}, *history[selected["cut"]:]]
        return original_send(text, timeout=timeout)

    provider.send = send
