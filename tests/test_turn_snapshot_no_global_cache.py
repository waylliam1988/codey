"""TurnSnapshot 必须贯穿发送/解析/执行，禁止全局缓存。"""
from __future__ import annotations

from codey.operations import kernel_protocol as proto
from codey.policies.task_policy import TaskPolicy


def test_no_global_snapshot_cache_retains_policies() -> None:
    assert not hasattr(proto, "_SNAPSHOT_TOOL_NAMES"), "必须删除 _SNAPSHOT_TOOL_NAMES 全局缓存"


def test_normalize_turn_requires_explicit_snapshot() -> None:
    import inspect
    sig = inspect.signature(proto.normalize_turn)
    assert "snapshot" in sig.parameters, f"normalize_turn 必须显式接收 snapshot，实际参数 {list(sig.parameters)}"


def test_registry_change_takes_effect_next_turn_only() -> None:
    from codey.toolchain import tool_spec as spec_mod
    policy = TaskPolicy(grants=frozenset({"control", "project.read"}))
    snap = proto.build_turn_snapshot(SimpleNamespace_session(policy))
    names_before = tuple(snap.tool_names)
    # 注册自定义工具后，本轮快照不变，下一轮可见
    try:
        spec_mod.register_custom_tool("snap_probe_tool_xyz", grant="control",
                                      parameters=(("value", {"type": "integer"}),), required=("value",))
        snap2 = proto.build_turn_snapshot(SimpleNamespace_session(policy))
        assert "snap_probe_tool_xyz" not in names_before
        # control 授权下新工具应可见（下一轮）
        assert "snap_probe_tool_xyz" in tuple(snap2.tool_names)
    finally:
        spec_mod.unregister_custom_tool("snap_probe_tool_xyz")


class _Sess:
    def __init__(self, policy):
        self.policy = policy
        self.search_results = {}
        self.opened_sources = set()
        self.evidence = []


def SimpleNamespace_session(policy):
    return _Sess(policy)


def test_snapshot_freezes_nested_schema() -> None:
    # 快照应冻结本轮工具定义与参数 schema（含嵌套），注册表变化不影响本轮解析
    from codey.toolchain import tool_spec as spec_mod
    policy = TaskPolicy(grants=frozenset({"control"}))
    assert spec_mod.register_custom_tool(
        "nested_freeze_probe", grant="control",
        parameters=(("obj", {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"], "additionalProperties": False}),),
        required=("obj",),
    )
    try:
        sess = _Sess(policy)
        snap = proto.build_turn_snapshot(sess)
        # 篡改注册表定义（模拟本轮后注册表变化）：本轮快照解析应仍按旧定义拒绝 string
        old = dict(spec_mod.tool_specs()["nested_freeze_probe"].parameters)
        spec_mod.tool_specs()["nested_freeze_probe"] = spec_mod.tool_specs()["nested_freeze_probe"].__class__(
            name="nested_freeze_probe", grant="control",
            parameters=(("obj", {"type": "object", "properties": {"n": {"type": "string"}}, "required": ["n"], "additionalProperties": False}),),
            required=("obj",), executor="custom",
        )
        try:
            plan = proto.normalize_turn('{"tool":"nested_freeze_probe","args":{"obj":{"n":"not-int"}}}',
                                        policy=policy, snapshot=snap)
            assert getattr(plan, "protocol_error", ""), "快照应冻结嵌套 schema，本轮仍应拒绝 string"
        finally:
            # 恢复
            spec_mod.tool_specs()["nested_freeze_probe"] = spec_mod.tool_specs()["nested_freeze_probe"].__class__(
                name="nested_freeze_probe", grant="control",
                parameters=tuple(old.items()) if isinstance(old, dict) else tuple(old),
                required=("obj",), executor="custom",
            )
    finally:
        spec_mod.unregister_custom_tool("nested_freeze_probe")
