import importlib.util
import sys
import unittest

sys.path.insert(0, ".")
spec = importlib.util.spec_from_file_location(
    "t_direct_gen", "tests/test_direct_gen.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

suite = unittest.TestLoader().loadTestsFromTestCase(m.MasterPromptTest)
unittest.TextTestRunner(verbosity=2).run(suite)
