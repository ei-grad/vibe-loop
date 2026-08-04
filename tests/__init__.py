import sys

from . import _test_bootstrap, _test_environment


sys.modules["_test_environment"] = _test_environment
sys.modules["_test_bootstrap"] = _test_bootstrap
