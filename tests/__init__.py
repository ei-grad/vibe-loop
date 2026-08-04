from ._test_environment import isolate_test_environment


# Test-owned processes must not inherit runtime identity or Git controls.
isolate_test_environment()
