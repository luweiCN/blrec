from pathlib import Path
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.control.operations import ControlOperationJournal, ControlStepInput
from blrec.web.routers import control_operations


@pytest.fixture
def journal(tmp_path: Path, event_loop) -> Iterator[ControlOperationJournal]:
    value = ControlOperationJournal(tmp_path / 'control.sqlite3')
    event_loop.run_until_complete(value.open())
    old_journal = control_operations.journal
    control_operations.journal = value
    try:
        yield value
    finally:
        control_operations.journal = old_journal
        event_loop.run_until_complete(value.close())


@pytest.fixture
def client(journal: ControlOperationJournal) -> Iterator[TestClient]:
    api = FastAPI()
    api.include_router(control_operations.router, prefix='/api/v1')
    with TestClient(api) as value:
        yield value


def test_status_route_returns_operation_and_items(
    client: TestClient, journal: ControlOperationJournal, event_loop
) -> None:
    operation = event_loop.run_until_complete(
        journal.admit(
            lane='task-state',
            kind='start',
            target_key='100',
            steps=[ControlStepInput(key='100')],
        )
    )

    response = client.get('/api/v1/control-operations/{}'.format(operation.id))

    assert response.status_code == 200
    assert response.json()['id'] == operation.id
    assert response.json()['status'] == 'accepted'
    assert response.json()['steps'] == [
        {
            'key': '100',
            'generation': 1,
            'status': 'queued',
            'result': None,
            'errorCode': None,
        }
    ]


def test_status_route_returns_404_for_unknown_operation(client: TestClient) -> None:
    response = client.get('/api/v1/control-operations/missing')

    assert response.status_code == 404
