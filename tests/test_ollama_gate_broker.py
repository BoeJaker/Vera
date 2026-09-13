import asyncio
import json
import time

import pytest
from starlette.requests import Request

from vera.evolve import evolve_capabilities as evolve
from vera import ollama_gate_broker_client as client_module

pytestmark = pytest.mark.critical


def run(coro):
    return asyncio.run(coro)


def test_token_auth_is_constant_time_digest_and_caller_scoped(monkeypatch):
    token = 'private-token'
    monkeypatch.setattr(evolve, '_sandbox_pool', lambda: async_value({
        'one': {'name': 'sandbox-one',
                'gate_token_sha256': evolve._gate_token_digest(token)}}))
    reset = evolve.CALLER_KIND.set('sandbox_gate')
    credential = evolve.SANDBOX_GATE_TOKEN.set(token)
    try:
        assert run(evolve._gate_broker_authorized('sandbox-one'))
        evolve.SANDBOX_GATE_TOKEN.set('wrong')
        assert not run(evolve._gate_broker_authorized('sandbox-one'))
    finally:
        evolve.SANDBOX_GATE_TOKEN.reset(credential)
        evolve.CALLER_KIND.reset(reset)
    assert not run(evolve._gate_broker_authorized('sandbox-one'))


def async_value(value):
    async def get():
        return value
    return get()


def test_broker_lease_ids_are_opaque_and_cross_sandbox_release_is_refused(monkeypatch):
    tokens = {'sandbox-one': 'one', 'sandbox-two': 'two'}
    monkeypatch.setattr(evolve, '_sandbox_pool', lambda: async_value({
        name: {'name': name, 'gate_token_sha256': evolve._gate_token_digest(token)}
        for name, token in tokens.items()}))
    monkeypatch.setattr(evolve._orch, 'OLLAMA_INSTANCES', {'gpu': {'has_gpu': True}})
    monkeypatch.setattr(evolve._orch, 'COORD_REDIS', object())
    async def ensure(): return None
    async def acquire(*args, **kwargs):
        return {'key': 'vera:private:key', 'owner': kwargs['owner'], 'waited_s': 0}
    async def release(*args): return True
    monkeypatch.setattr(evolve._orch, '_ensure_coord_redis', ensure)
    monkeypatch.setattr(evolve._orch._gate, 'acquire', acquire)
    monkeypatch.setattr(evolve._orch._gate, 'release', release)
    monkeypatch.setattr(evolve._orch._gate, 'capacity_for', lambda gpu: 1)
    evolve._BROKER_LEASES.clear()

    async def exercise():
        reset = evolve.CALLER_KIND.set('sandbox_gate')
        credential = evolve.SANDBOX_GATE_TOKEN.set('one')
        try:
            acquired = await evolve.ollama_gate_lease_acquire(
                sandbox='sandbox-one', node='gpu', wait_s=0)
            assert acquired['ok'] and 'lease_id' in acquired
            assert 'key' not in acquired and 'owner' not in acquired
            evolve.SANDBOX_GATE_TOKEN.set('two')
            refused = await evolve.ollama_gate_lease_release(
                sandbox='sandbox-two', lease_id=acquired['lease_id'])
            assert refused == {'ok': False, 'error': 'unknown_lease'}
            evolve.SANDBOX_GATE_TOKEN.set('one')
            released = await evolve.ollama_gate_lease_release(
                sandbox='sandbox-one', lease_id=acquired['lease_id'])
            assert released == {'ok': True, 'released': True}
        finally:
            evolve.SANDBOX_GATE_TOKEN.reset(credential)
            evolve.CALLER_KIND.reset(reset)
    run(exercise())


def test_pause_cleanup_releases_only_named_sandbox(monkeypatch):
    evolve._BROKER_LEASES.clear()
    evolve._BROKER_LEASES.update({
        'a': {'sandbox': 'one', 'lease': {'owner': 'a'}, 'created': 1},
        'b': {'sandbox': 'two', 'lease': {'owner': 'b'}, 'created': 2},
    })
    released = []
    async def release(redis, lease):
        released.append(lease['owner'])
        return True
    monkeypatch.setattr(evolve._orch, 'COORD_REDIS', object())
    monkeypatch.setattr(evolve._orch._gate, 'release', release)
    assert run(evolve._gate_broker_release_sandbox('one')) == 1
    assert released == ['a']
    assert set(evolve._BROKER_LEASES) == {'b'}


def test_generated_compose_exposes_broker_not_coordination_redis():
    rendered = evolve._dev_compose_yaml('worktree', name='sandbox-one',
                                        port=8980, db=3, gate_token='token')
    assert 'VERA_COORD_REDIS_URL: "off"' in rendered
    assert 'VERA_GATE_BROKER_URL: "https://host.docker.internal:8999/mcp/call"' in rendered
    assert 'VERA_GATE_BROKER_SANDBOX: "sandbox-one"' in rendered
    assert 'VERA_GATE_BROKER_TOKEN: "token"' in rendered


def test_client_rejects_non_https_and_redacts_transport_error(monkeypatch):
    with pytest.raises(client_module.BrokerError, match='invalid_broker_configuration'):
        client_module.BrokerClient('http://controller/mcp/call', 'sandbox', 'token')

    class FailingClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            raise RuntimeError('token=private-token')
    monkeypatch.setattr(client_module.httpx, 'AsyncClient', FailingClient)
    broker = client_module.BrokerClient('https://controller/mcp/call', 'sandbox', 'token')
    with pytest.raises(client_module.BrokerError) as error:
        run(broker.acquire('gpu', 0))
    assert str(error.value) == 'broker_unreachable'
    assert 'private-token' not in str(error.value)


def test_client_credential_is_header_only(monkeypatch):
    captured = {}
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {'content': {'ok': True, 'lease_id': 'opaque'}}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            captured.update(kwargs)
            return Response()
    monkeypatch.setattr(client_module.httpx, 'AsyncClient', Client)
    broker = client_module.BrokerClient('https://controller/mcp/call', 'sandbox', 'secret')
    assert run(broker.acquire('gpu', 0))['broker_lease_id'] == 'opaque'
    assert captured['headers'] == {'X-Vera-Sandbox-Gate': 'secret'}
    assert 'token' not in captured['json']['arguments']


def test_lost_broker_renewal_cancels_the_generation_holder(monkeypatch):
    class Broker:
        async def renew(self, lease, ttl): return False
        async def release(self, lease): return True
    monkeypatch.setattr(evolve._orch, '_GATE_BROKER', Broker())
    monkeypatch.setattr(evolve._orch._gate, 'renew_interval_s', lambda: 0.001)
    monkeypatch.setattr(evolve._orch._gate, 'lease_ttl_ms', lambda: 100)

    async def exercise():
        holder = asyncio.create_task(asyncio.sleep(60))
        activity = {'t': time.monotonic(), 'beats': 1}
        await evolve._orch._gate_heartbeat(
            {'broker_lease_id': 'opaque'}, activity=activity, holder_task=holder)
        assert activity['gate_lost'] is True
        with pytest.raises(asyncio.CancelledError):
            await holder
    run(exercise())


def test_mcp_header_enters_scoped_context_and_is_reset():
    name = 'test.sandbox.gate.header'
    existing = evolve._orch.CAPABILITY_REGISTRY.get(name)
    try:
        @evolve._orch.capability(name, memory='off', mcp_expose=False)
        async def observe(trace_id=None):
            return {'credential': evolve._orch.SANDBOX_GATE_TOKEN.get()}

        payload = json.dumps({'name': name, 'arguments': {},
                              'caller_kind': 'sandbox_gate'}).encode()
        sent = False
        async def receive():
            nonlocal sent
            if sent:
                return {'type': 'http.disconnect'}
            sent = True
            return {'type': 'http.request', 'body': payload, 'more_body': False}
        request = Request({
            'type': 'http', 'method': 'POST', 'path': '/mcp/call',
            'headers': [(b'x-vera-sandbox-gate', b'header-secret')]}, receive)
        response = run(evolve._orch._make_mcp_call_handler()(request))
        decoded = json.loads(response.body)
        assert decoded['content'] == {'credential': 'header-secret'}
        assert evolve._orch.SANDBOX_GATE_TOKEN.get() == ''
    finally:
        if existing is None:
            evolve._orch.CAPABILITY_REGISTRY.pop(name, None)
        else:
            evolve._orch.CAPABILITY_REGISTRY[name] = existing
