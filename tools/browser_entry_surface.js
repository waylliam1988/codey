// Experiment adapter. Each action still runs through the frozen host.
const browser = Object.fromEntries(Object.entries(options.methods).map(([name, tool]) => [name, async (args) => {
  let result;
  let snapshot;
  if (options.targets && ['fill', 'click', 'upload', 'download'].includes(name)) {
    const observed = await hostTools.browser_snapshot({});
    if (!observed.ok) result = observed;
    else {
      const page = unwrap(observed);
      snapshot = {page_id:page.page_id, generation:page.generation};
      const {target_name, role, ...action} = args;
      const matches = page.elements.filter(e => e.name === target_name && (role === undefined || e.role === role));
      if (matches.length === 1) result = await hostTools[tool]({...action, ref:matches[0].ref});
      else result = {ok:false, error:JSON.stringify({
        code:matches.length ? 'target_ambiguous' : 'target_missing', target_name, role, snapshot,
        candidates:(matches.length ? matches : page.elements).slice(0, 16).map(({name,role}) => ({name,role})),
      })};
    }
  } else if (name === 'property') {
    if (typeof args.name !== 'string' || !/^[A-Za-z_$][A-Za-z0-9_$]*$/.test(args.name)) {
      result = {ok:false,error:'Expected one JavaScript property identifier'};
    } else result = await hostTools[tool]({code:'window.' + args.name});
  } else {
    result = await hostTools[tool](args);
  }
  if (!result.ok) return options.results ? {status:'failed', error:result.error, ...(snapshot && {snapshot})} : result;
  const value = unwrap(result);
  let data = value;
  if (name === 'url' || name === 'property') {
    const item = name === 'url' ? value.page_url : value.value;
    data = {value:item, type:item === null ? 'null' : Array.isArray(item) ? 'array' : typeof item};
    if (name === 'property') data.truncated = value.truncated;
  }
  if (options.results) {
    return {status:'succeeded', data:typeof data === 'string' ? {text:data} : data,
      ...(result.result_ref && {receipt_ref:result.result_ref}),
      available_views:tool === 'browser_read' ? ['output'] : ['output','value']};
  }
  return name === 'url' || name === 'property' ? {...result, value:data} : result;
}]));
