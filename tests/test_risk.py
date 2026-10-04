import pytest
from app.risk import summarize

ADDR="0x1111111111111111111111111111111111111111"

def wrap(item):
    return {"result": {ADDR: item}}

def test_honeypot_high():
    r=summarize(wrap({"is_honeypot":"1"}), ADDR)
    assert r["risk"]=="high" and "honeypot" in r["flags"]

def test_mintable_caution():
    r=summarize(wrap({"is_mintable":"1"}), ADDR)
    assert r["risk"]=="caution"

def test_clean_is_not_called_safe():
    r=summarize(wrap({"is_honeypot":"0","is_mintable":"0"}), ADDR)
    assert r["risk"]=="no_flag_detected"
    assert "proof of safety" in r["disclaimer"]

def test_missing_unknown():
    r=summarize({"result":{}}, ADDR)
    assert r["risk"]=="unknown"

def test_metadata_without_risk_fields_remains_unknown():
    r=summarize(wrap({'token_name':'Example'}),ADDR)
    assert r['risk']=='unknown' and r['status']=='insufficient_evidence'

def test_blank_risk_flags_do_not_become_negative_evidence():
    r=summarize(wrap({'is_honeypot':'','is_mintable':None}),ADDR)
    assert r['risk']=='unknown'

def test_partial_negative_evidence_discloses_missing_fields():
    r=summarize(wrap({'is_honeypot':'0'}),ADDR)
    assert r['risk']=='no_flag_detected'
    assert r['evidence_coverage']['observed_risk_fields']==['is_honeypot']
    assert 'is_mintable' in r['evidence_coverage']['missing_risk_fields']

@pytest.mark.parametrize('raw',[None,[],{'result':[]},{'result':{ADDR:'invalid'}}])
def test_malformed_direct_input_stays_unknown(raw):
    assert summarize(raw,ADDR)['risk']=='unknown'
