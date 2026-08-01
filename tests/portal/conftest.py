from __future__ import annotations

import pytest

from portal.application.service import PortalService
from portal.repository.memory import InMemoryPortalRepository


@pytest.fixture
def repository() -> InMemoryPortalRepository:
    return InMemoryPortalRepository()


@pytest.fixture
def service(repository: InMemoryPortalRepository) -> PortalService:
    return PortalService(repository)
