import asyncio
from importlib import import_module
from fastapi.testclient import TestClient
import httpx
import pytest
from app.main import app

client = TestClient(app)

def test_health_contract():
    r = client.get('/health')
    assert r.status_code == 200
    assert r.json() == {'status':'ok','service':'ORACL3 Token Preflight','version':'0.1.1','x402_enabled':False}

def test_root_opens_existing_api_docs():
    r = client.get('/', follow_redirects=False)
    assert r.status_code == 307 and r.headers['location'] == '/docs'

def test_invalid_chain_rejected_before_upstream():
    r = client.get('/v1/token-preflight/not-a-chain/0x0000000000000000000000000000000000000000')
    assert r.status_code == 400

def test_invalid_address_rejected_before_upstream():
    r = client.get('/v1/token-preflight/1/not-an-address')
    assert r.status_code == 400

ADDR = '0x1111111111111111111111111111111111111111'

def upstream(monkeypatch, handler):
    actual = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(handler), **kw))

@pytest.mark.parametrize('exc,status,detail', [
    (httpx.ReadTimeout('sensitive diagnostic'),504,'security upstream timed out'),
    (httpx.ConnectError('sensitive diagnostic'),502,'security upstream could not be reached'),
])
def test_upstream_transport_failure_is_controlled(monkeypatch, exc, status, detail):
    def handler(request):
        raise exc
    upstream(monkeypatch, handler)
    r = client.get(f'/v1/token-preflight/1/{ADDR}')
    assert r.status_code == status and r.json() == {'detail':detail}
    assert 'sensitive diagnostic' not in r.text

@pytest.mark.parametrize('response,detail', [
    (httpx.Response(200,text='not JSON'),'security upstream returned invalid JSON'),
    (httpx.Response(200,json=[]),'security upstream returned an invalid response'),
    (httpx.Response(200,json={'code':4000,'message':'sensitive diagnostic'}),'security upstream reported a provider error'),
    (httpx.Response(200,json={'code':1,'result':[] }),'security upstream returned an invalid token record'),
    (httpx.Response(200,json={'code':1,'result':{ADDR:'invalid'}}),'security upstream returned an invalid token record'),
])
def test_upstream_payload_failure_is_controlled(monkeypatch,response,detail):
    upstream(monkeypatch, lambda request:response)
    r = client.get(f'/v1/token-preflight/1/{ADDR}')
    assert r.status_code == 502 and r.json() == {'detail':detail}

def test_upstream_http_error_is_not_a_risk_assessment(monkeypatch):
    upstream(monkeypatch, lambda request:httpx.Response(429))
    r = client.get(f'/v1/token-preflight/1/{ADDR}')
    assert r.status_code == 502 and r.json()['detail'] == 'security upstream returned HTTP 429'

def test_no_record_is_unknown_not_gateway_error(monkeypatch):
    upstream(monkeypatch, lambda request:httpx.Response(200,json={'code':1,'result':{}}))
    r = client.get(f'/v1/token-preflight/1/{ADDR}')
    assert r.status_code == 200 and r.json()['risk'] == 'unknown'

def test_case_preserved_record_is_summarized(monkeypatch):
    address='0xabcdefabcdefabcdefabcdefabcdefabcdefabcd'
    upstream(monkeypatch, lambda request:httpx.Response(200,json={'code':1,'result':{address.upper():{'is_honeypot':'1'}}}))
    r = client.get(f'/v1/token-preflight/1/{address}')
    assert r.status_code == 200 and r.json()['risk'] == 'high'

def test_total_upstream_budget_cancels_slow_request(monkeypatch):
    main = import_module('app.main')
    monkeypatch.setattr(main, 'UPSTREAM_TOTAL_TIMEOUT_SECONDS', 0.01)
    cancelled = []

    async def handler(request):
        try:
            await asyncio.sleep(1)
            return httpx.Response(200, json={'code': 1, 'result': {}})
        finally:
            cancelled.append(True)

    upstream(monkeypatch, handler)
    r = client.get(f'/v1/token-preflight/1/{ADDR}')
    assert r.status_code == 504
    assert r.json() == {'detail': 'security upstream timed out'}
    assert cancelled == [True]

@pytest.mark.parametrize('chain,address', [
    ('١', ADDR),
    ('1', ADDR + '%0A'),
])
def test_malformed_path_never_reaches_upstream(monkeypatch, chain, address):
    def unexpected(request):
        pytest.fail('invalid path reached external provider')
    upstream(monkeypatch, unexpected)
    r = client.get(f'/v1/token-preflight/{chain}/{address}')
    assert r.status_code == 400
