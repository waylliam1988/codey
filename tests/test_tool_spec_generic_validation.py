"""通用 schema 校验只检查声明的 schema，不混入 edit 兼容与隐式转换。"""
from __future__ import annotations


def _register(name: str, params, required):
    from codey.toolchain import tool_spec as m
    m.unregister_custom_tool(name)
    assert m.register_custom_tool(name, grant="control", parameters=params, required=required)
    return m


def test_custom_object_rejects_edit_alias_keys() -> None:
    m = _register("alias_probe_tool", (( "obj", {"type": "object", "properties": {"old_string": {"type": "string"}}, "required": ["old_string"], "additionalProperties": False}),), ("obj",))
    try:
        err = m.validate_args_against_spec("alias_probe_tool", {"obj": {"search": "value"}})
        assert err, "通用对象校验混入 edit 别名 search->old_string，应拒绝额外字段 search"
    finally:
        m.unregister_custom_tool("alias_probe_tool")


def test_custom_integer_enforces_maximum_or_rejects_unsupported() -> None:
    from codey.toolchain import tool_spec as m
    name = "max_probe_tool"
    m.unregister_custom_tool(name)
    ok = m.register_custom_tool(name, grant="control",
                                parameters=(("value", {"type": "integer", "maximum": 5}),),
                                required=("value",))
    try:
        if not ok:
            # 注册时拒绝不支持的约束也是合法的（禁止注册成功但忽略约束）
            return
        err = m.validate_args_against_spec(name, {"value": 100})
        assert err, "maximum=5 时 value=100 应被拒绝（或注册时已拒绝）"
    finally:
        m.unregister_custom_tool(name)


def test_custom_integer_rejects_numeric_string_without_coercion() -> None:
    m = _register("intstr_probe_tool", (("value", {"type": "integer"}),), ("value",))
    try:
        err = m.validate_args_against_spec("intstr_probe_tool", {"value": "100"})
        assert err, "数字字符串不应通过整数检查（执行器收到的类型必须与 schema 相符）"
    finally:
        m.unregister_custom_tool("intstr_probe_tool")


def test_custom_integer_rejects_bool_and_float() -> None:
    m = _register("intbool_probe_tool", (("value", {"type": "integer"}),), ("value",))
    try:
        assert m.validate_args_against_spec("intbool_probe_tool", {"value": True})
        assert m.validate_args_against_spec("intbool_probe_tool", {"value": 1.5})
    finally:
        m.unregister_custom_tool("intbool_probe_tool")
