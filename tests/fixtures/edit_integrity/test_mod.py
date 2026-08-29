def test_value():
    import importlib
    import sys
    sys.path.insert(0, "src")
    mod = importlib.import_module("mod")
    assert mod.VALUE == 2
