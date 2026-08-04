if __package__:
    from ._test_environment import configure_test_environment
else:
    from _test_environment import configure_test_environment


configure_test_environment()
