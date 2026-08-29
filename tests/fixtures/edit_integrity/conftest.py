# The edit-integrity fixtures are path-shape samples, not runnable tests:
# test_mod.py exists to pin the "tests/test_mod.py" classification and
# intentionally asserts against a module whose VALUE never becomes 2.
collect_ignore_glob = ["test_mod.py"]
