from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health_contract():
    r = client.get('/health')
    assert r.status_code == 200
    assert r.json() == {'status':'ok','service':'ORACL3 Token Preflight','version':'0.1.0'}

def test_invalid_chain_rejected_before_upstream():
    r = client.get('/v1/token-preflight/not-a-chain/0x0000000000000000000000000000000000000000')
    assert r.status_code == 400

def test_invalid_address_rejected_before_upstream():
    r = client.get('/v1/token-preflight/1/not-an-address')
    assert r.status_code == 400
