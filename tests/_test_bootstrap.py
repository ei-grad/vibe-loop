if __package__:
    from ._test_environment import isolate_test_environment
else:
    from _test_environment import isolate_test_environment


isolate_test_environment()
# Test modules import this marker so bootstrap execution is an explicit dependency.
TEST_ENVIRONMENT_CONFIGURED = True
