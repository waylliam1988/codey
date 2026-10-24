"""External data uses the shared tools and never weakens CodeMode isolation."""
from types import SimpleNamespace

import pytest

from codey.codemode.protocol import parse_codemode_args
from codey.operations.kernel_prompt import kernel_prompt_for_session
from codey.policies.command_line import ShellSpec, resolve_shell


@pytest.mark.parametrize('native', [False, True])
def test_shell_lookup_guidance_is_capability_driven_not_topic_driven(native):
    session = SimpleNamespace(task_text='Get current public information', handoff='', project='workspace', shell=resolve_shell())
    prompt = kernel_prompt_for_session(session, tool_names=('shell', 'codemode', 'done'), native=native)
    assert 'explicit connection and total timeouts' in prompt
    assert 'Do not launch a browser' in prompt
    assert 'Keep shell approval intact' in prompt
    assert 'not evidence that the host has no network access' in prompt
    restricted = kernel_prompt_for_session(session, tool_names=('codemode', 'done'), native=native)
    assert 'shell can request' not in restricted


def test_codemode_rejection_explains_the_scope_without_executing_network_code():
    with pytest.raises(ValueError, match='not a host network denial'):
        parse_codemode_args({'code': 'return await fetch("https://example.com")'})


@pytest.mark.parametrize('native',[False,True])
@pytest.mark.parametrize('tool',['run','shell'])
def test_command_guidance_declares_actual_windows_executor(monkeypatch,native,tool):
    from codey.operations import kernel_prompt
    monkeypatch.setattr(kernel_prompt.sys,'platform','win32')
    session=SimpleNamespace(task_text='Assess project',handoff='',project='workspace',
                            shell=ShellSpec('cmd',r'C:\Windows\System32\cmd.exe',None,'noninteractive','0'*64))
    prompt=kernel_prompt_for_session(session,tool_names=(tool,'done'),native=native)
    if tool == 'shell':
        assert 'Execution host platform: win32' in prompt
        assert r'C:\Windows\System32\cmd.exe' in prompt
        assert 'Bash heredocs' in prompt
        assert 'python -c' in prompt
        assert 'invoke PowerShell explicitly' in prompt
    else:
        assert 'directly as argv, without a shell' in prompt
        assert 'Bash heredocs' not in prompt
