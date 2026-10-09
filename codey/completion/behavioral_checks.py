"""Bounded admission of explicit behavioral requirements; no model or I/O."""
from __future__ import annotations

import hashlib
import json
import re
import string
from dataclasses import dataclass

from codey.completion.contract import CompletionCheck


@dataclass(frozen=True)
class BehavioralPlan:
    task_digest: str
    requirement_quote: str
    path: str
    function: str
    pairs: tuple[tuple[str, str], ...]
    digest: str


@dataclass(frozen=True)
class BehavioralObservation:
    plan_digest: str
    workspace_fingerprint: str
    status: str
    reason: str
    output_ref: str = ''
    summary: str = ''


def behavioral_completion_check(plan: BehavioralPlan | None, observation: BehavioralObservation | None,
                                 task: str, fingerprint: str) -> CompletionCheck | None:
    if plan is None:
        return None
    reason = ''
    if hashlib.sha256(task.encode()).hexdigest() != plan.task_digest:
        reason = 'behavioral_task_changed'
    elif observation is None or observation.plan_digest != plan.digest:
        reason = 'behavioral_unobserved'
    elif not fingerprint or observation.workspace_fingerprint != fingerprint:
        reason = 'behavioral_stale'
    elif not observation.output_ref:
        reason = 'behavioral_receipt_missing'
    elif observation.status not in {'pass', 'fail', 'not_run'}:
        reason = 'behavioral_result_invalid'
    if reason:
        return CompletionCheck('behavioral_verification', 'not_run', reason)
    assert observation is not None
    return CompletionCheck('behavioral_verification', observation.status,
                           '' if observation.status == 'pass' else 'behavioral_' + observation.reason)


def admit_behavioral_plan(task: str, targets: tuple[tuple[str, str], ...]) -> BehavioralPlan | None:
    """Only unqualified ASCII deletion; this is not general NL understanding.

    The operation is selected from the original request by a closed grammar,
    never from a model-selected rule, implementation, or writer summary.
    """
    matches = list(re.finditer(r'\b(?:remove|removes|removing) ASCII punctuation\b', task, re.I))
    if len(matches) != 1:
        return None
    # Later exceptions also invalidate the unconditional rule. Unsupported
    # conditional requests remain proposals; never ask the model to admit them.
    if re.search(r'\b(?:except|unless|provided|when|if)\b|\bdo (?:that|this) only\b', task, re.I):
        return None
    if re.search(r'\b(?:preserve|keep|leave)\b[^.;\n]*\b(?:periods?|commas?|hyphens?|dashes?|'
                 r'underscores?|quotes?|brackets?|parentheses|punctuation)\b', task, re.I):
        return None
    match = matches[0]
    boundaries = list(re.finditer(r'[.!?;](?=\s|$)|\n', task))
    start = max((b.end() for b in boundaries if b.end() <= match.start()), default=0)
    end = min((b.start() for b in boundaries if b.start() >= match.end()), default=len(task))
    if re.search(r'\b(?:replace|replaces|keep|preserve|treat)\b[^.;\n]*\bpunctuation\b', task, re.I):
        return None
    # Bind within this requirement, not another operation elsewhere in the task.
    # Filename dots are not sentence boundaries. Require an explicit callable;
    # a module's only supported function need not be the requested operation.
    prefix = task[start:match.start()]
    suffix = re.split(r'[,;\n]|\band\b', task[match.end():end], maxsplit=1)[0]
    binding = prefix
    named = [target for target in targets if re.search(r'\b' + re.escape(target[1]) + r'\b', binding)]
    if not named:
        named = [target for target in targets if re.search(r'\b' + re.escape(target[1]) + r'\b', suffix)]
    if len(named) != 1:
        return None
    path, function = named[0]
    if not re.fullmatch(r'(?:[A-Za-z_][\w-]*/)*[A-Za-z_][\w-]*\.py', path) or not function.isidentifier():
        return None
    subject = re.sub(r'\b' + re.escape(function) + r'\b', 'FUNCTION', prefix).replace(path, 'FILE').strip()
    head = (r'(?:(?:fix|verify|implement|update|check|ensure)\s+)?'
            r'(?:FILE\s*(?::|so)\s*FUNCTION|FUNCTION(?:\s+in\s+FILE)?|in\s+FILE,\s*FUNCTION)'
            r'(?:\s+(?:should|must))?'
            r'(?:\s+lowercases ASCII letters,|\s+already satisfies lowercase,)?')
    tail = task[match.end():end].strip()
    property_clause = (r'(?:lowercas(?:e|es|ing) ASCII letters|preserv(?:e|es|ing) digits|'
                       r'trim(?:s|ming)? outer whitespace|collaps(?:e|es|ing) whitespace'
                       r'(?: into (?:hyphens|underscores))?)')
    other_properties = r'(?:(?:,\s*(?:and\s+)?|and\s+)' + property_clause + r'\s*)*'
    direct = re.fullmatch(head, subject, re.I) and re.fullmatch(other_properties, tail, re.I)
    imperative = subject.lower() in {'verify', 'check', 'ensure'} and tail == 'in ' + function
    if not (direct or imperative) or (end < len(task) and task[end] == '?'):
        return None
    pairs = tuple(('Qr7t', 'Q' + punctuation + 'r7t') for punctuation in string.punctuation)
    task_digest = hashlib.sha256(task.encode()).hexdigest()
    quote = matches[0].group()
    digest = hashlib.sha256(json.dumps([task_digest, quote, path, function, pairs],
                                      ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return BehavioralPlan(task_digest, quote, path, function, pairs, digest)
