import json, sys, tempfile, threading, shutil, copy
from pathlib import Path
from http.client import HTTPConnection
sys.path.insert(0, '.')
from docgov.server import make_server
from docgov.manifest import prepare_candidate
from docgov.canonical import sign
from tests.acceptance import make_manifest, page

d = Path(tempfile.mkdtemp(prefix="gate-smoke-"))
srv = make_server("127.0.0.1", 0, str(d/"g.db"), str(d/"data"))
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

def req(method, path, body=None):
    c = HTTPConnection("127.0.0.1", port, timeout=10)
    data = json.dumps(body) if body is not None else None
    h = {"Content-Type": "application/json"} if data else {}
    c.request(method, path, data, h)
    r = c.getresponse()
    raw = r.read().decode()
    return r.status, json.loads(raw) if raw else {}

# baseline
gov = srv.gov
m1 = make_manifest(1, [page("index.md", "# v1"), page("old.md", "old page")],
                   assets=["assets/logo.png"])
gov.set_asset_licence("assets/logo.png", "LICENSED")
p1 = prepare_candidate(copy.deepcopy(m1))
st, r = req("POST", "/api/releases/submit",
            {"manifest": p1.manifest, "signature": sign(p1.manifest, gov.author_key)})
pk1 = r["id"]
req("POST", f"/api/releases/{pk1}/evaluate", {})
req("POST", f"/api/releases/{pk1}/approve", {})
req("POST", f"/api/releases/{pk1}/prepare", {})
st, r = req("POST", f"/api/releases/{pk1}/publish", {"idem_token": "t1"})
assert st == 200 and r["gen"], r

# two managers
def submit_ready(mf):
    p = prepare_candidate(copy.deepcopy(mf))
    st, r = req("POST", "/api/releases/submit",
                {"manifest": p.manifest, "signature": sign(p.manifest, gov.author_key)})
    pk = r["id"]
    req("POST", f"/api/releases/{pk}/evaluate", {})
    req("POST", f"/api/releases/{pk}/approve", {})
    req("POST", f"/api/releases/{pk}/prepare", {})
    return pk

mA = submit_ready(make_manifest(2, [page("index.md", "# A"), page("old.md","old page")], assets=["assets/logo.png"]))
mB = submit_ready(make_manifest(2, [page("index.md", "# B"), page("old.md","old page")], assets=["assets/logo.png"]))
results = {}
bar = threading.Barrier(2)
def pub(pk, tok, k):
    bar.wait()
    results[k] = req("POST", f"/api/releases/{pk}/publish", {"idem_token": tok})
threading.Thread(target=pub, args=(mA,"TA","A")).start()
threading.Thread(target=pub, args=(mB,"TB","B")).start()
import time; time.sleep(1)
print("A:", results["A"][0], results["A"][1].get("error") or results["A"][1]["gen"][:10])
print("B:", results["B"][0], results["B"][1].get("error") or results["B"][1]["gen"][:10])
assert sorted([results["A"][0], results["B"][0]]) == [200, 409]
assert results["A"][1].get("error") == "BASELINE_DRIFT" or results["B"][1].get("error") == "BASELINE_DRIFT"

# idempotent repeat on the winner with the SAME token as the first call
winner_pk, winner_tok = (mB, "TB") if results["B"][0] == 200 else (mA, "TA")
st2, r3 = req("POST", f"/api/releases/{winner_pk}/publish", {"idem_token": winner_tok})
print("repeat:", st2, r3.get("idempotent"), r3.get("gen","")[:10])
assert r3["idempotent"] is True

# console + blockers locator render
c = HTTPConnection("127.0.0.1", port)
c.request("GET", "/")
r = c.getresponse(); html = r.read().decode()
assert r.status == 200 and "发布清单" in html
c.request("GET", "/app.js")
r = c.getresponse(); js = r.read().decode()
assert r.status == 200 and "refresh" in js
print("HTML/JS console served OK,", len(html), "bytes html,", len(js), "bytes js")
srv.shutdown()
shutil.rmtree(d, ignore_errors=True)
print("HTTP smoke passed")
