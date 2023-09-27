from dotenv import load_dotenv

import pytest


@pytest.fixture(scope="session", autouse=True)
def load_test_env():
    load_dotenv('test-local.env')