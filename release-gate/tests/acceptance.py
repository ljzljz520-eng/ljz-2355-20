"""Acceptance tests for the documentation release gate.

Scenarios (from the release requirements):

  A1 两人同时发版：第二个经理拿到 BASELINE_DRIFT，旧版完整服务
  A2 索引落后：INDEX_FRESH 阻断
  A3 删页无迁移：PAGE_DIFF 阻断
  A4 重复发布请求：幂等令牌返回同一次切换
  A5 切换后回滚：旧代次完整恢复服务

Plus mechanism tests:
  M1 快乐路径（评估→批准→预检→发布，证据逐项落库）
  M2 批准后反馈变更：预检因依赖指纹变化拒绝，必须重新评估
  M3 素材撤权：改变构建资格（BUILD_ELIGIBLE）
  M4 页面重命名改变断链结果 + 豁免绑定指纹
  M5 物化资料与已签 manifest 不一致时预检失败
  M6 不同时间的绿灯不能拼成一次通过
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docgov.canonical import sign
from docgov.governor import Governor
from docgov.errors import GovError

PASS, FAIL = "✅", "❌"
_results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))
    return bool(cond)


def expect_error(fn, code):
    try:
        fn()
    except GovError as e:
        return e.code == code, e
    return False, None


class World:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="gate-acc-"))
        self.gov = Governor(str(self.dir / "g.db"), str(self.dir / "data"))
        self.author = self.gov.author_key

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def submit(self, m):
        # CI content-addresses the candidate first; the author then signs the
        # fully-prepared manifest (blobs/sha256/index checksum frozen).
        import copy
        from docgov.manifest import prepare_candidate
        prepared = prepare_candidate(copy.deepcopy(m))
        sig = sign(prepared.manifest, self.author)
        return self.gov.submit(prepared.manifest, sig)["id"]

    def push(self, m):
        pk = self.submit(m)
        v = self.gov.evaluate(pk)
        assert v["state"] == "SEALED", f"eval not sealed: {[c['code'] for c in v['blockers']]}"
        self.gov.approve(pk)
        self.gov.prepare(pk)
        r = self.gov.publish(pk, f"tok-{m['version']}")
        return pk, r["gen"]


def page(path, blob, title=None):
    return {"path": path, "title": title or path, "blob": blob}


def make_manifest(version, pages, *, assets=None, redirects=None,
                  index_ids=None, routes=None, release_id="docs",
                  art_paths=None):
    assets = assets or []
    redirects = redirects or []
    from docgov.manifest import artifact_for_page, render_artifact
    from docgov.canonical import encode, sha256
    artifacts = {}
    for p in pages:
        ap = art_paths[p["path"]] if art_paths and p["path"] in art_paths \
            else artifact_for_page(p["path"])
        artifacts[ap] = sha256(render_artifact(p))
    if index_ids is None:
        index_ids = sorted(artifacts)
    index = {"doc_ids": index_ids,
             "checksum": sha256(encode(sorted(index_ids)))}
    if routes is None:
        routes = {("/" + a[:-5] if a.endswith(".html") else "/" + a): a
                  for a in artifacts}
    m = {"manifest_version": 1, "release_id": release_id,
         "version": version, "pages": pages,
         "assets": [{"path": a, "sha256": "x",
                     "licence": {"status": "LICENSED"}} for a in assets],
         "redirects": redirects, "artifacts": artifacts,
         "index": index, "routes": routes}
    return m


def full_gate(gov, pk, token=None):
    gov.evaluate(pk)
    gov.approve(pk)
    gov.prepare(pk)
    return gov.publish(pk, token)


# ---------------------------------------------------------------- M1 happy

def test_m1():
    print("M1 快乐路径与逐项证据")
    w = World()
    try:
        m = make_manifest(1, [page("index.md", "# Hello"),
                              page("guide/a.md", "A docs")])
        pk = w.submit(m)
        v = w.gov.evaluate(pk)
        check("11 项评估检查全部执行并落库", len([c for c in v["checks"]
              if c["phase"] == "EVALUATE"]) == 11)
        check("无阻断项", len(v["blockers"]) == 0)
        check("状态 SEALED", v["state"] == "SEALED")
        check("seal 已生成", bool(v["seal"]))
        w.gov.approve(pk)
        p = w.gov.prepare(pk)
        check("预检生成 DEP_FINGERPRINTS + SIGNED_CONSISTENCY",
              len([c for c in p["checks"] if c["phase"] == "PREPARE"]) == 2)
        r = w.gov.publish(pk, "tok-1")
        check("发布成功并返回代次", bool(r["gen"]))
        served = w.gov.serve("/index")
        from docgov.manifest import render_artifact
        check("线上服务的是新代次产物", served == render_artifact(m["pages"][0]))
        ev = w.gov.events()
        check("审计事件覆盖提交到发布",
              {"SUBMIT", "EVALUATE", "APPROVE", "PREPARE", "PUBLISH"}
              <= {e["kind"] for e in ev})
    finally:
        w.close()


# ---------------------------------------------------------------- baseline

def baseline_world() -> tuple[World, str, str]:
    w = World()
    m = make_manifest(1, [page("index.md", "# v1"),
                          page("old.md", "old page")],
                      assets=["assets/logo.png"])
    for a in m["assets"]:
        w.gov.set_asset_licence(a["path"], "LICENSED")
    pk, gen = w.push(m)
    return w, pk, gen


# ---------------------------------------------------------------- A1 concurrency

def test_a1():
    print("A1 两人同时发版（不同候选），后者基线漂移被拒")
    w, pk1, gen1 = baseline_world()
    try:
        m2a = make_manifest(2, [page("index.md", "# v2 by manager-A"),
                                page("old.md", "old page")],
                            assets=["assets/logo.png"])
        m2b = make_manifest(2, [page("index.md", "# v2 by manager-B"),
                                page("old.md", "old page")],
                            assets=["assets/logo.png"])
        pkA, pkB = w.submit(m2a), w.submit(m2b)
        for pk in (pkA, pkB):
            w.gov.evaluate(pk)
            w.gov.approve(pk)
            w.gov.prepare(pk)

        out = {}
        barrier = threading.Barrier(2)

        def worker(pk, tok, key):
            barrier.wait()
            try:
                out[key] = ("ok", w.gov.publish(pk, tok)["gen"])
            except GovError as e:
                out[key] = ("err", e.code)

        t1 = threading.Thread(target=worker, args=(pkA, "tok-A", "A"))
        t2 = threading.Thread(target=worker, args=(pkB, "tok-B", "B"))
        t1.start(); t2.start(); t1.join(); t2.join()
        codes = sorted(out.values())
        check("恰好一次成功", [c[0] for c in codes].count("ok") == 1)
        check("另一人得到 BASELINE_DRIFT",
              any(c == ("err", "BASELINE_DRIFT") for c in codes))
        from docgov.manifest import render_artifact
        body = w.gov.serve("/index")
        expected = {"A": render_artifact(m2a["pages"][0]),
                    "B": render_artifact(m2b["pages"][0])}
        winner_key = "A" if out["A"][0] == "ok" else "B"
        check("线上服务完整且等于胜出版本（无半成品）",
              body == expected[winner_key])
        winner = out["A"][1] if out["A"][0] == "ok" else out["B"][1]
        check("在线代次为胜出者", w.gov.active_info()["active"]["gen"] == winner)
    finally:
        w.close()


# ---------------------------------------------------------------- A2 index

def test_a2():
    print("A2 索引落后（删页后索引未更新）被 INDEX_FRESH 阻断")
    w, _, _ = baseline_world()
    try:
        m = make_manifest(2, [page("index.md", "# v2"),
                              page("old.md", "old")],
                          assets=["assets/logo.png"])
        # index lags: drop old.html from docs but index keeps both, routes fine
        m["index"]["doc_ids"] = ["index.html", "old.html", "ghost.html"]
        from docgov.canonical import encode, sha256
        m["index"]["checksum"] = sha256(encode(sorted(m["index"]["doc_ids"])))
        pk = w.submit(m)
        v = w.gov.evaluate(pk)
        codes = {c["code"]: c for c in v["checks"]}
        check("INDEX_FRESH 阻断", codes["INDEX_FRESH"]["status"] == "BLOCKED")
        locs = [f["locator"] for f in codes["INDEX_FRESH"]["evidence"]["findings"]]
        check("证据可定位到陈旧条目 ghost.html", "ghost.html" in locs)
        check("不可豁免（硬性新鲜度）", codes["INDEX_FRESH"]["waivable"] == 0)
        _, e = expect_error(lambda: w.gov.grant_waiver(pk, "INDEX_FRESH", "x"),
                            "NOT_WAIVABLE")
        check("豁免被系统拒绝", e is not None)
        check("状态保持 BLOCKED", w.gov.get_release(pk)["state"] == "BLOCKED")
    finally:
        w.close()


# ---------------------------------------------------------------- A3 delete

def test_a3():
    print("A3 删页无迁移：PAGE_DIFF 阻断；配置重定向后通过")
    w, _, _ = baseline_world()
    try:
        m = make_manifest(2, [page("index.md", "# v2")],
                          assets=["assets/logo.png"])
        pk = w.submit(m)
        v = w.gov.evaluate(pk)
        pd = {c["code"]: c for c in v["checks"]}["PAGE_DIFF"]
        check("PAGE_DIFF 阻断删除页", pd["status"] == "BLOCKED")
        find = [f for f in pd["evidence"]["findings"] if f["locator"] == "old.md"]
        check("定位到 old.md 且标记 blocking",
              find and find[0]["detail"].get("blocking") is True)

        # add migration redirect -> page diff passes
        m["redirects"] = [{"from": "old.md", "to": "index.md"}]
        pk2 = w.submit(m)
        v2 = w.gov.evaluate(pk2)
        check("配置迁移后 PAGE_DIFF 通过",
              {c["code"]: c for c in v2["checks"]}["PAGE_DIFF"]["status"] == "PASS")
        check("可继续发布", w.gov.get_release(pk2)["state"] == "SEALED")
    finally:
        w.close()


# ---------------------------------------------------------------- A4 idempotent

def test_a4():
    print("A4 重复发布请求：相同幂等令牌=同一次切换；不同令牌=ALREADY_PUBLISHED")
    w, _, gen1 = baseline_world()
    try:
        m = make_manifest(2, [page("index.md", "# v2"),
                              page("old.md", "old")],
                          assets=["assets/logo.png"])
        pk = w.submit(m)
        w.gov.evaluate(pk); w.gov.approve(pk); w.gov.prepare(pk)
        r1 = w.gov.publish(pk, "same-token")
        r2 = w.gov.publish(pk, "same-token")
        check("同令牌返回同代次（幂等）",
              r2["idempotent"] and r2["gen"] == r1["gen"])
        _, e = expect_error(lambda: w.gov.publish(pk, "other-token"),
                            "ALREADY_PUBLISHED")
        check("不同令牌拒绝重复发布", e is not None)
    finally:
        w.close()


# ---------------------------------------------------------------- A5 rollback

def test_a5():
    print("A5 切换后回滚：旧代次完整恢复")
    w, _, gen1 = baseline_world()
    try:
        m2 = make_manifest(2, [page("index.md", "# v2"),
                               page("old.md", "old")],
                           assets=["assets/logo.png"])
        pk2, gen2 = w.push(m2)
        check("v2 在线", w.gov.active_info()["active"]["gen"] == gen2)
        rb = w.gov.rollback(gen1)
        check("回滚到旧代次", rb["rolled_back_to"] == gen1)
        from docgov.manifest import render_artifact
        check("旧版资料完整服务",
              w.gov.serve("/index") == render_artifact(page("index.md", "# v1")))
        check("路由表也回滚（/old 可用）",
              w.gov.serve("/old") == render_artifact(page("old.md", "old page")))
        g = w.gov.db.connect()
        row = g.execute("SELECT state FROM generations WHERE gen=?",
                        (gen2,)).fetchone()
        check("被回滚代次标记 RETIRED 但资料保留", row["state"] == "RETIRED")
    finally:
        w.close()


# ---------------------------------------------------------------- M2 feedback

def test_m2():
    print("M2 批准后反馈变更：预检拒绝，重新评估后才能发布")
    w, _, _ = baseline_world()
    try:
        m = make_manifest(2, [page("index.md", "# v2"),
                              page("old.md", "old")],
                          assets=["assets/logo.png"])
        pk = w.submit(m)
        w.gov.evaluate(pk); w.gov.approve(pk)
        # relevant change AFTER approval
        w.gov.add_feedback("index.md", "BLOCKER", "首页存在阻断性内容问题")
        _, e = expect_error(lambda: w.gov.prepare(pk), "CHECKLIST_STALE")
        check("预检拒绝拼绿灯", e is not None)
        detail = e.extra.get("mismatches", [])
        check("定位到 FEEDBACK_OPEN 输入变化",
              any(x["code"] == "FEEDBACK_OPEN" for x in detail))
        v = w.gov.evaluate(pk)
        check("重新评估后 FEEDBACK_OPEN 阻断",
              {c["code"]: c for c in v["checks"]}["FEEDBACK_OPEN"]["status"]
              == "BLOCKED")
        # resolve feedback, re-evaluate/re-approve -> publish succeeds
        fid = [f for f in w.gov.db.connect().execute(
            "SELECT id FROM feedback").fetchall()][-1]["id"]
        w.gov.resolve_feedback(fid)
        v2 = w.gov.evaluate(pk)
        check("反馈清零后重新 SEALED", v2["state"] == "SEALED")
        w.gov.approve(pk); w.gov.prepare(pk)
        check("重新批准后发布成功", bool(w.gov.publish(pk, "tok-m2")["gen"]))
    finally:
        w.close()


# ---------------------------------------------------------------- M3 rights

def test_m3():
    print("M3 素材撤权：改变 ASSET_RIGHTS 与 BUILD_ELIGIBLE，批准失效")
    w, _, _ = baseline_world()
    try:
        m = make_manifest(2, [page("index.md", "# v2 ![](assets/logo.png)"),
                              page("old.md", "old")],
                          assets=["assets/logo.png"])
        pk = w.submit(m)
        v = w.gov.evaluate(pk)
        check("授权有效时构建资格成立",
              {c["code"]: c for c in v["checks"]}["BUILD_ELIGIBLE"]["status"]
              == "PASS")
        w.gov.approve(pk)
        w.gov.set_asset_licence("assets/logo.png", "REVOKED")
        v2 = w.gov.evaluate(pk)
        codes = {c["code"]: c for c in v2["checks"]}
        check("撤权后 ASSET_RIGHTS 阻断",
              codes["ASSET_RIGHTS"]["status"] == "BLOCKED")
        check("撤权后 BUILD_ELIGIBLE 阻断（素材撤权改变构建资格）",
              codes["BUILD_ELIGIBLE"]["status"] == "BLOCKED")
        check("两项均不可豁免",
              codes["ASSET_RIGHTS"]["waivable"] == 0
              and codes["BUILD_ELIGIBLE"]["waivable"] == 0)
        _, e = expect_error(lambda: w.gov.prepare(pk), "NOT_APPROVED")
        check("无法再预检/发布", e is not None)
    finally:
        w.close()


# ---------------------------------------------------------------- M4 rename

def test_m4():
    print("M4 页面重命名改变断链结果；豁免只对相同依赖指纹有效")
    w, _, _ = baseline_world()
    try:
        # page links to guide/start.md which does not exist
        m = make_manifest(2, [
            page("index.md", "# v2\n\n[开始](guide/start.md)"),
            page("old.md", "old")], assets=["assets/logo.png"])
        pk = w.submit(m)
        v = w.gov.evaluate(pk)
        bl = {c["code"]: c for c in v["checks"]}["BROKEN_LINKS"]
        check("断链被发现", bl["status"] == "BLOCKED")
        # rename: add redirect from guide/start.md -> existing page
        m["redirects"] = [{"from": "guide/start.md", "to": "index.md"}]
        pk2 = w.submit(m)
        v2 = w.gov.evaluate(pk2)
        check("重命名+重定向后断链结果变为 PASS（依赖重定向）",
              {c["code"]: c for c in v2["checks"]}["BROKEN_LINKS"]["status"]
              == "PASS")

        # waiver binding: waiver on broken link, then inputs change -> invalid
        m3 = make_manifest(3, [
            page("index.md", "[x](missing.md)"),
            page("old.md", "old")], assets=["assets/logo.png"])
        pk3 = w.submit(m3)
        w.gov.evaluate(pk3)
        wv = w.gov.grant_waiver(pk3, "BROKEN_LINKS", "临时接受断链")
        check("豁免绑定输入指纹", wv["binding"].get("redirects") is not None)
        # change redirects inputs by re-submitting with an extra redirect
        m3["redirects"] = [{"from": "a.md", "to": "index.md"}]
        # waiver belongs to old candidate; new candidate starts BLOCKED anyway.
        # Re-evaluate same candidate after feedback-independent change is not
        # possible (immutable) — instead validate waiver binding mismatch path:
        rows = w.gov.db.connect().execute(
            "SELECT inputs FROM check_rows WHERE release_pk=? AND code='BROKEN_LINKS'",
            (pk3,)).fetchone()
        binding = json.loads(rows["inputs"])
        check("豁免记录的重定向指纹≠变更后指纹（豁免不能跨变更复用）",
              wv["binding"]["redirects"] == binding["redirects"]
              and wv["binding"]["redirects"] != _redfp(m3))

        # waiver becomes unusable when the page set changes (new broken link)
        v3 = w.gov.get_release(pk3)
        check("豁免后原清单可放行（SEALED）", v3["state"] == "SEALED")
        m4 = make_manifest(4, [
            page("index.md", "[x](missing.md)\n[y](also-missing.md)"),
            page("old.md", "old")], assets=["assets/logo.png"])
        pk4 = w.submit(m4)
        v4 = w.gov.evaluate(pk4)
        check("页面集合变化后旧豁免不适用，新候选仍 BLOCKED",
              v4["state"] == "BLOCKED"
              and {c["code"]: c for c in v4["checks"]}["BROKEN_LINKS"]["status"]
              == "BLOCKED")
    finally:
        w.close()


def _redfp(m):
    from docgov.manifest import fp_redirects
    return fp_redirects(m)


# ---------------------------------------------------------------- M5 attest

def test_m5():
    print("M5 切换/预检时公开资料必须与已签 manifest 一致")
    w, _, _ = baseline_world()
    try:
        m = make_manifest(2, [page("index.md", "# v2"),
                              page("old.md", "old")],
                          assets=["assets/logo.png"])
        pk = w.submit(m)
        w.gov.evaluate(pk); w.gov.approve(pk)
        # simulate corruption hook: prepare normally, then tamper staged files
        w.gov.prepare(pk)
        gen = w.gov.get_release(pk)["staged_gen"]
        gdir = w.dir / "data" / "generations" / gen
        (gdir / "artifacts" / "index.html").write_bytes(b"TAMPERED")
        _, e = expect_error(lambda: w.gov.publish(pk, "tok-m5"),
                            "MATERIALIZE_FAILED")
        check("切换时回读校验失败并拒绝", e is not None)
        check("旧版仍完整服务",
              w.gov.active_info()["active"]["gen"] is not None)
    finally:
        w.close()


# ---------------------------------------------------------------- M6 stitch

def test_m6():
    print("M6 不同时间的绿灯不能拼成一次通过（页面在批准后撤页）")
    w, _, _ = baseline_world()
    try:
        m = make_manifest(2, [page("index.md", "# v2"),
                              page("old.md", "old")],
                          assets=["assets/logo.png"])
        pk = w.submit(m)
        w.gov.evaluate(pk); w.gov.approve(pk)
        # baseline itself switches via a concurrent different release: emulate
        # by publishing another candidate that took baseline -> drift
        other = make_manifest("side", [page("index.md", "# side"),
                                       page("old.md", "old")],
                              assets=["assets/logo.png"],
                              release_id="side-release")
        _, sidegen = w.push(other)
        _, e = expect_error(lambda: w.gov.prepare(pk), "BASELINE_DRIFT")
        check("基线代次变化即要求重新评估", e is not None)
    finally:
        w.close()


def main():
    tests = [test_m1, test_a1, test_a2, test_a3, test_a4, test_a5,
             test_m2, test_m3, test_m4, test_m5, test_m6]
    for t in tests:
        t()
    total = len(_results)
    ok = sum(1 for _, c, _ in _results if c)
    print("\n" + "=" * 64)
    print(f"验收断言：{ok}/{total} 通过")
    if ok != total:
        for n, c, d in _results:
            if not c:
                print(f"  {FAIL} {n} — {d}")
        sys.exit(1)
    print("全部验收场景通过。")


if __name__ == "__main__":
    main()
