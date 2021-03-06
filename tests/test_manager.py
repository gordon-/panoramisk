# -*- coding: utf-8 -*-
import os
import pytest
from panoramisk import testing
from panoramisk import message
from panoramisk.utils import asyncio

test_dir = os.path.join(os.path.dirname(__file__), 'fixtures')


@pytest.fixture
def manager():
    def manager(stream=None, **config):
        if stream:
            config['stream'] = os.path.join(test_dir, stream)
        return testing.Manager(**config)
    return manager


def test_connection(manager):
    assert isinstance(manager().connect(), asyncio.Task)


def test_ping(manager):
    manager = manager(stream='ping.yaml')
    future = manager.send_action({'Action': 'Ping'})
    assert 'ping' in future.result()


def test_login_ok(manager):
    manager = manager(username='xx', secret='xx', stream='login_ok.yaml')
    assert manager.authenticated_future.result().success is True
    assert manager.login(manager.authenticated_future) is True


def test_login_failed(manager):
    manager = manager(username='xx', secret='xx', stream='login_failed.yaml')
    assert manager.authenticated_future.result().success is False
    assert manager.login(manager.authenticated_future) is False


def test_logoff(manager):
    manager = manager(stream='logoff.yaml')
    future = manager.send_action({'Action': 'logoff'})
    assert future.result().success is True


def test_queue_status(manager):
    manager = manager(stream='queue_status.yaml')
    future = manager.send_action({'Action': 'QueueStatus',
                                  'Queue': 'xxxxxxxxxxxxxxxx-tous'})
    responses = future.result()
    assert len(responses) == 9


def test_get_variable(manager):
    manager = manager(stream='get_variable.yaml')
    future = manager.send_action({'Command': 'GET VARIABLE "PEERNAME"'})
    responses = future.result()
    assert len(responses) == 2


def test_asyncagi_get_variable(manager):
    manager = manager(stream='asyncagi_get_var.yaml')
    future = manager.send_command({'Command': 'GET VARIABLE endpoint'},
                                  agi=True)
    response = future.result()
    assert response.result == '200 result=1 (SIP/000000)'


def test_originate_sync(manager):
    manager = manager(stream='originate_sync.yaml')
    future = manager.send_action({'Action': 'Originate', 'Async': 'false'})
    response = future.result()
    assert response.success
    assert response.message == 'Originate successfully queued'


def test_close(manager):
    manager().close()


def test_events(manager):
    future = asyncio.Future()

    def callback(event, manager):
        future.set_result(event)

    manager = manager()
    manager.register_event('Peer*', callback)
    event = message.Message.from_line('Event: PeerStatus')
    assert event.success is True
    assert event['Event'] == 'PeerStatus'
    assert 'Event' in event
    matches = manager.dispatch(event)
    assert matches == ['Peer*']
    assert event is future.result()

    event = message.Message.from_line('Event: NoPeerStatus')
    matches = manager.dispatch(event)
    assert matches == []
