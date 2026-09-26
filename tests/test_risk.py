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
