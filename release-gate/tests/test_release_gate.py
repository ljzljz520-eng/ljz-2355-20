"""Acceptance scenarios (runnable with plain `python -m unittest`):

  1. happy path, two-person sign-off, same-generation switch
  2. two simultaneous publish requests (idempotency + conflict)
  3. index lag blocks release
  4. deleted page without migration blocks; rename resolves broken links
  5. repeat/duplicate publish request
  6. post-switch rollback keeps old generation intact
  7. change after approval forces re-evaluation (no time-shifted green lights)
  8. dependency fingerprints: rename -> broken_links; revoke -> build eligibility
  9. long-transaction drift aborts the run
  10. final public materials equal the signed manifest
  11. frontend/API locates blockers precisely
  12. waivers are scope-bound and invalidated on change
"""
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

import rg.evaluate as ev
from fixtures import make_site
from helpers import Env
from rg import publish as pb
from rg.publish import PublishError
from rg.service import ConflictError


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = Env(self.tmp)

    def tearDown(self):
        pb.FAULT_HOOK.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # 1 ------------------------------------------------------------- happy --
    def test_01_happy_path_two_person_switch(self):
        self.env.publish_first()
        self.assertTrue(self.env.svc.verify_public()["consistent"])
        site = make_site(self.tmp / "v2")
        self.env.svc.create_release("rel-v2", str(site))
        r = self.env.svc.evaluate("rel-v2")
        self.assertEqual(r["state"], "committed")
        self.assertTrue(r["passed"])

        self.env.svc.approve("rel-v2", "alice")
        with self.assertRaisesRegex(PublishError, "两人"):
            self.env.svc.publish("rel-v2", str(site / "dist"), "k1")
        self.env.svc.approve("rel-v2", "bob")

        out = self.env.svc.publish("rel-v2", str(site / "dist"), "k1")
        self.assertEqual(out["generation"], 2)
        pub = self.env.svc.verify_public()
        self.assertTrue(pub["consistent"])
        self.assertEqual(pub["release_id"], "rel-v2")

    # 2 ------------------------------------------------------- simultaneous --
    def test_02_simultaneous_same_release_is_idempotent(self):
        self.env.publish_first()
        site = make_site(self.tmp / "v2")
        self.env.full_publish("rel-v2", site)
        results, errors, barrier = [], [], threading.Barrier(2)

        def fire():
            try:
                barrier.wait()
                results.append(self.env.svc.publish(
                    "rel-v2", str(site / "dist"), "same-key"))
            except Exception as e:  # noqa
                errors.append(e)

        ts = [threading.Thread(target=fire) for _ in range(2)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertFalse(errors)
        self.assertEqual({r.get("generation") for r in results}, {2})
        # both requests resolve to the single switched generation, never gen-3
        self.assertTrue(all(r.get("duplicate") for r in results))
        self.assertEqual(
            self.env.svc.verify_public()["active_generation"], 2)

    def test_02b_concurrent_different_release_conflict(self):
        # active = rel-base. Two DIFFERENT releases each change the site and
        # are fully approved; they must not switch generations at the same time.
        minimal = {"index.md": ("首页", "x")}
        base = make_site(self.tmp / "base", pages=minimal, first=True)
        self.env.full_publish("rel-base", base)

        pages_a = {"index.md": ("首页", "x"), "a.md": ("A 页", "a")}
        pages_b = {"index.md": ("首页", "x"), "b.md": ("B 页", "b")}
        a = make_site(self.tmp / "a", pages=pages_a, first=True,
                      redirects={}, migrations={"deleted_pages": []})
        b = make_site(self.tmp / "b", pages=pages_b, first=True,
                      redirects={}, migrations={"deleted_pages": []})
        for rid, site in (("rel-a", a), ("rel-b", b)):
            self.env.svc.create_release(rid, str(site))
            self.env.svc.evaluate(rid)
            self.env.svc.approve(rid, "alice")
            self.env.svc.approve(rid, "bob")

        entered, release = threading.Event(), threading.Event()

        def hook(conn, gen, staged):
            entered.set(); release.wait(2)
        pb.FAULT_HOOK["after_switch"] = hook
        got = {}

        def slow_a():
            try:
                self.env.svc.publish("rel-a", str(a / "dist"), "re-a")
            except ConflictError as e:
                got["a"] = e

        def publish_b():
            entered.wait(2)
            try:
                self.env.svc.publish("rel-b", str(b / "dist"), "re-b")
            except ConflictError as e:
                got["b"] = e

        t1 = threading.Thread(target=slow_a); t1.start()
        time.sleep(0.15)
        t2 = threading.Thread(target=publish_b); t2.start()
        t2.join(3); release.set(); t1.join(3)
        self.assertIn("b", got, "并发切换不同发布必须让后来者收到冲突")

    # 3 ---------------------------------------------------------- index lag --
    def test_03_index_lag_blocks(self):
        self.env.publish_first()
        site = make_site(self.tmp / "v2")
        (site / "dist" / "search-index.json").write_text(json.dumps(
            {"routes": ["/", "/installation", "/old-guide"]}))
        self.env.svc.create_release("rel-lag", str(site))  # manifest rebuilt
        self.env.svc.evaluate("rel-lag")
        names = {b["name"] for b in self.env.svc.gate("rel-lag")["blockers"]}
        self.assertIn("index_coverage", names)
        with self.assertRaises((PublishError, ValueError)):
            self.env.svc.approve("rel-lag", "alice")

    # 4 ------------------------------------------------ delete/rename cases --
    def test_04_deleted_page_without_migration_blocks(self):
        self.env.publish_first()
        pages = {"index.md": ("首页", "x"),
                 "installation.md": ("安装", "x")}
        site = make_site(self.tmp / "v2", pages=pages,
                         redirects={}, migrations={"deleted_pages": []})
        self.env.svc.create_release("rel-del", str(site))
        self.env.svc.evaluate("rel-del")
        names = {b["name"] for b in self.env.svc.gate("rel-del")["blockers"]}
        self.assertIn("deleted_migration", names)

    def test_04b_rename_changes_broken_link_result(self):
        self.env.publish_first()
        pages = {"index.md": ("首页", "欢迎\n\n[新指南](/quickstart)\n"),
                 "installation.md": ("安装", "安装\n"),
                 "quickstart.md": ("快速开始", "内容\n")}
        site = make_site(
            self.tmp / "v2", pages=pages,
            redirects={"/old-guide": "/quickstart"},
            migrations={"deleted_pages": [
                {"route": "/old-guide", "redirect_to": "/quickstart",
                 "migrated": True}]})
        self.env.svc.create_release("rel-ren", str(site))
        r = self.env.svc.evaluate("rel-ren")
        statuses = {i["name"]: i["status"] for i in r["items"]}
        for n in ("page_diff", "deleted_migration", "redirect",
                  "broken_links", "routes_table", "index_coverage"):
            self.assertEqual(statuses[n], "pass", n)

        pages_broken = dict(pages)
        pages_broken["index.md"] = ("首页",
                                    "欢迎\n\n[旧指南](/old-guide)\n")
        site2 = make_site(self.tmp / "v2b", pages=pages_broken,
                          redirects={}, migrations={"deleted_pages": []})
        self.env.svc.create_release("rel-ren2", str(site2))
        self.env.svc.evaluate("rel-ren2")
        blockers = {b["name"]: b
                    for b in self.env.svc.gate("rel-ren2")["blockers"]}
        self.assertIn("broken_links", blockers)
        self.assertEqual(
            blockers["broken_links"]["evidence"]["broken"][0]["target"],
            "/old-guide")

    # 5 ------------------------------------------------------------- dupes --
    def test_05_duplicate_publish_same_generation(self):
        rid, v1 = self.env.publish_first()
        first = self.env.svc.publish(rid, str(v1 / "dist"), "dup-key")
        self.assertEqual(first["generation"], 1)
        self.assertTrue(first["duplicate"])
        second = self.env.svc.publish(rid, str(v1 / "dist"), "dup-key")
        self.assertEqual(second["generation"], 1)
        self.assertTrue(second["duplicate"])
        third = self.env.svc.publish(rid, str(v1 / "dist"), "new-key")
        self.assertEqual(third["generation"], 1)
        self.assertTrue(third["duplicate"])
        self.assertEqual(
            self.env.svc.verify_public()["active_generation"], 1)

    # 6 ----------------------------------------------------------- rollback --
    def test_06_rollback_keeps_old_intact(self):
        self.env.publish_first()
        site = make_site(self.tmp / "v2")
        self.env.full_publish("rel-v2", site)
        rb = self.env.svc.rollback()
        self.assertEqual((rb["from"], rb["rolled_back_to"]), (2, 1))
        pub = self.env.svc.verify_public()
        self.assertTrue(pub["consistent"])
        self.assertEqual(pub["active_generation"], 1)
        again = self.env.svc.rollback(target_generation=2)
        self.assertEqual(again["rolled_back_to"], 2)

    def test_06b_switch_failure_auto_rolls_back(self):
        self.env.publish_first()
        site = make_site(self.tmp / "v2")
        self.env.svc.create_release("rel-fail", str(site))
        self.env.svc.evaluate("rel-fail")
        self.env.svc.approve("rel-fail", "alice")
        self.env.svc.approve("rel-fail", "bob")

        def boom(conn, gen, staged):
            raise RuntimeError("simulated post-switch failure")
        pb.FAULT_HOOK["after_switch"] = boom
        with self.assertRaises(RuntimeError):
            self.env.svc.publish("rel-fail", str(site / "dist"), "fail-key")
        pub = self.env.svc.verify_public()
        self.assertTrue(pub["consistent"])
        self.assertEqual(pub["active_generation"], 1)

    # 7 -------------------------------------------- post-approval re-evaluate
    def test_07_change_after_approval_invalidates(self):
        self.env.publish_first()
        site = make_site(self.tmp / "v2")
        self.env.svc.create_release("rel-chg", str(site))
        self.env.svc.evaluate("rel-chg")
        self.env.svc.approve("rel-chg", "alice")
        self.env.svc.approve("rel-chg", "bob")

        lics = json.loads((site / "licenses.json").read_text())
        lics["assets/button-demo.png"]["revoked"] = True
        (site / "licenses.json").write_text(json.dumps(lics))

        self.env.svc.evaluate("rel-chg")
        appr = self.env.svc.gate("rel-chg")["approvals"]
        self.assertEqual(len(appr["stale_approvals"]), 2)
        self.assertFalse(appr["approved"])
        with self.assertRaisesRegex(PublishError, "两人"):
            self.env.svc.publish("rel-chg", str(site / "dist"), "chg-key")

    def test_07b_green_lights_cannot_be_spliced(self):
        self.env.publish_first()
        site = make_site(self.tmp / "v2")
        self.env.svc.create_release("rel-split", str(site))
        fp1 = self.env.svc.evaluate("rel-split")["aggregate_fingerprint"]
        (site / "src" / "quickstart.md").write_text("# 改了\n",
                                                     encoding="utf-8")
        r2 = self.env.svc.evaluate("rel-split")
        self.assertNotEqual(fp1, r2["aggregate_fingerprint"])
        self.assertEqual(self.env.svc.gate("rel-split")["run_id"],
                         r2["run_id"])

    # 8 --------------------------------------------- dependency fingerprints --
    def test_08_rename_cascade_fingerprint(self):
        self.env.publish_first()
        site = make_site(self.tmp / "v2")
        self.env.svc.create_release("rel-dep", str(site))
        fps = {i["name"]: i["depends_fprint"]
               for i in self.env.svc.evaluate("rel-dep")["items"]}
        site2 = make_site(self.tmp / "v2c",
                          redirects={"/x": "/button"})
        self.env.svc.create_release("rel-dep2", str(site2))
        fps2 = {i["name"]: i["depends_fprint"]
                for i in self.env.svc.evaluate("rel-dep2")["items"]}
        self.assertNotEqual(fps["redirect"], fps2["redirect"])
        self.assertNotEqual(fps["broken_links"], fps2["broken_links"])

    def test_08b_revoked_asset_blocks_eligibility(self):
        self.env.publish_first()
        site = make_site(self.tmp / "v2")
        lics = json.loads((site / "licenses.json").read_text())
        lics["assets/logo.svg"]["revoked"] = True
        (site / "licenses.json").write_text(json.dumps(lics))
        self.env.svc.create_release("rel-rev", str(site))
        self.env.svc.evaluate("rel-rev")
        blockers = {b["name"]
                    for b in self.env.svc.gate("rel-rev")["blockers"]}
        self.assertIn("asset_license", blockers)
        self.assertIn("build_eligibility", blockers)

    # 9 --------------------------------------------------------- drift txn --
    def test_09_long_transaction_drift_aborts(self):
        self.env.publish_first()
        site = make_site(self.tmp / "v2")
        self.env.svc.create_release("rel-drift", str(site))
        orig = ev.tree_checksum
        state = {"n": 0}

        def flaky(root):
            state["n"] += 1
            if state["n"] == 2:
                (site / "src" / "index.md").write_text("# 中途修改\n",
                                                        encoding="utf-8")
            return orig(root)
        ev.tree_checksum = flaky
        try:
            r = self.env.svc.evaluate("rel-drift")
            self.assertEqual(r["state"], "aborted_drift")
            self.assertFalse(r["passed"])
            with self.assertRaisesRegex((PublishError, ValueError), "漂移"):
                self.env.svc.approve("rel-drift", "alice")
        finally:
            ev.tree_checksum = orig

    # 10 ---------------------------------------------- public == signed ------
    def test_10_tampered_public_material_detected(self):
        self.env.publish_first()
        site = make_site(self.tmp / "v2")
        self.env.full_publish("rel-v2", site)
        link = self.tmp / "store" / "active"
        (link / "index.html").write_text("tampered", encoding="utf-8")
        report = self.env.svc.verify_public()
        self.assertFalse(report["consistent"])
        self.assertTrue(report["artifact_errors"])

    # 11 ------------------------------------------------- API/frontend ------
    def test_11_api_locates_blockers(self):
        from rg.server import serve
        self.env.publish_first()
        site = make_site(self.tmp / "v2")
        (site / "feedback.json").write_text(json.dumps(
            [{"id": "fb-1", "page": "/button", "state": "open",
              "text": "示例报错"}], ensure_ascii=False))
        self.env.svc.create_release("rel-api", str(site))
        self.env.svc.evaluate("rel-api")
        httpd = serve(self.env.svc, 0)
        port = httpd.server_address[1]
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/gate/rel-api") as resp:
                g = json.loads(resp.read())
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/") as resp:
                self.assertIn("发布门禁", resp.read().decode())
        finally:
            httpd.shutdown()
        blocker = next(b for b in g["blockers"] if b["name"] == "feedback")
        self.assertEqual(
            blocker["evidence"]["open_on_changed_pages"][0]["id"], "fb-1")

    # 12 ---------------------------------------------------------- waivers --
    def test_12_waiver_scope_bound_and_invalidated(self):
        self.env.publish_first()
        site = make_site(self.tmp / "v2")
        lics = json.loads((site / "licenses.json").read_text())
        del lics["assets/logo.svg"]["license"]
        (site / "licenses.json").write_text(json.dumps(lics))
        self.env.svc.create_release("rel-w", str(site))
        self.env.svc.evaluate("rel-w")
        self.assertTrue(any(b["name"] == "asset_license"
                            for b in self.env.svc.gate("rel-w")["blockers"]))

        self.env.svc.waiver("rel-w", "asset_license", "授权邮件已在路上", "legal")
        self.env.svc.evaluate("rel-w")
        gate = self.env.svc.gate("rel-w")
        self.assertFalse(gate["blockers"])
        self.assertTrue(any(i["name"] == "asset_license"
                            and i["status"] == "waived"
                            for i in gate["items"]))

        # relevant change + manifest rebuild => the waiver goes out of scope
        lics["assets/logo.svg"] = {"license": "MIT"}
        (site / "licenses.json").write_text(json.dumps(lics))
        self.env.svc.create_release("rel-w", str(site))
        self.env.svc.evaluate("rel-w")
        rows = self.env.svc.conn().execute(
            "SELECT active FROM waivers WHERE release_id='rel-w'").fetchall()
        self.assertTrue(rows and all(r[0] == 0 for r in rows))

    def test_13_partial_failure_before_switch_keeps_old(self):
        self.env.publish_first()
        site = make_site(self.tmp / "v2")
        self.env.svc.create_release("rel-pf", str(site))
        self.env.svc.evaluate("rel-pf")
        self.env.svc.approve("rel-pf", "alice")
        self.env.svc.approve("rel-pf", "bob")
        (site / "dist" / "index.html").write_text("different bytes later")
        with self.assertRaisesRegex(PublishError, "暂存校验"):
            self.env.svc.publish("rel-pf", str(site / "dist"), "pf-key")
        self.assertEqual(
            self.env.svc.verify_public()["active_generation"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
