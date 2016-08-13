from unittest.mock import Mock

import pytest
from bloop import Client, Engine

from ..helpers.models import BaseModel


@pytest.fixture
def engine():
    engine = Engine()
    engine.client = Mock(spec=Client)
    engine.bind(base=BaseModel)
    return engine
