// Experiment-only interpreter. No mutation cache: historical success is not current page state.
const completed = [];
const snapshots = {};
const fileReads = new Map();
for (const step of inputs.steps) {
  try {
    let name = step.op;
    let args = {...step.args};
    if (inputs.action_tools.includes(name)) {
      const snapshot = args.snapshot_id ? snapshots[args.snapshot_id] : unwrap(await tools.browser_snapshot({}));
      if (!snapshot) throw Error('snapshot_missing: use an earlier snapshot step in this plan');
      const matches = snapshot.elements.filter(e => e.name === args.target_name);
      if (matches.length !== 1) {
        return {completed, failed_step:step.id, error:{
          code:matches.length ? 'target_ambiguous' : 'target_missing', requested_name:args.target_name,
          page_id:snapshot.page_id, generation:snapshot.generation,
          candidates:snapshot.elements.slice(0, 20).map(e => ({name:e.name,role:e.role})),
        }};
      }
      args.ref = matches[0].ref;
      delete args.target_name;
      delete args.snapshot_id;
    }
    if (name === 'browser_property') {
      name = 'browser_eval';
      args = {code:'window.' + args.name};
    }
    if (name === 'edit') {
      const read = args.same_plan_read ? fileReads.get(args.read_handle) : inputs.reads[args.read_handle];
      if (!read) throw Error('read_not_delivered: first read the file in a separate plan');
      args = {path:read.path, base_result_ref:read.result_ref, content:args.content};
    }
    const result = await tools[name](args);
    if (!result.ok) {
      completed.push({id:step.id,op:step.op,status:'failed',error:result.error});
      if (step.allow_failure === true) continue;
      return {completed,failed_step:step.id,error:result.error};
    }
    const value = unwrap(result);
    const data = typeof value === 'string' ? {text:value} : value;
    completed.push({id:step.id,op:step.op,status:'succeeded',data,receipt_ref:result.result_ref ?? null});
    if (name === 'browser_snapshot') snapshots[step.id] = data;
    if (name === 'read_file') fileReads.set(step.id, {path:value.path, result_ref:result.result_ref});
  } catch (error) {
    return {completed,failed_step:step.id,error:String(error.message ?? error)};
  }
}
return {completed,failed_step:null};
