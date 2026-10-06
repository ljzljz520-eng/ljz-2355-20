import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rg.service import Service  # noqa
from rg import publish as pb  # noqa
from fixtures import make_site, baseline_live_pages  # noqa

SECRET = b"test-secret"


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.db = str(tmp / "registry.db")
        self.store = str(tmp / "store")
        # isolate any CLI-default state
        import os
        os.environ["RG_DB"] = self.db
        os.environ["RG_STORE"] = self.store
        self.svc = Service(self.db, self.store, SECRET)

    def publish_first(self, name="v1"):
        """Establish a live generation so subsequent candidates diff against it."""
        site = make_site(self.tmp / name, pages=baseline_live_pages())
        rid = f"rel-{name}"
        self.svc.create_release(rid, str(site))
        self.svc.evaluate(rid)
        self.svc.approve(rid, "alice")
        self.svc.approve(rid, "bob")
        self.svc.publish(rid, str(site / "dist"), f"key-{name}")
        return rid, site

    def full_publish(self, rid, site):
        self.svc.create_release(rid, str(site))
        self.svc.evaluate(rid)
        self.svc.approve(rid, "alice")
        self.svc.approve(rid, "bob")
        return self.svc.publish(rid, str(site / "dist"), f"key-{rid}")
