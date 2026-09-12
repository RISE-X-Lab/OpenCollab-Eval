import json
import multiprocessing as mp
import tempfile
import threading
import time
import unittest
from pathlib import Path

from provider_limits_support import fill_worker, one_worker

from opencollab_eval.commands.provider_limits import Cancelled, ProviderLimits


class LimitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "policy.json"
        self.config.write_text(
            json.dumps(
                {
                    "lock_root": str(self.root / "locks"),
                    "providers": {
                        "company": {"name": "company", "origin": "http://company", "max_active": 45},
                        "laboratory": {"name": "laboratory", "origin": "http://laboratory", "max_active": 30},
                    },
                }
            )
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_three_processes_share_company45_laboratory30(self):
        ctx = mp.get_context("spawn")
        ready = ctx.Queue()
        release = ctx.Event()
        workers = [ctx.Process(target=fill_worker, args=(str(self.config), ready, release)) for _ in range(3)]
        for p in workers:
            p.start()
        try:
            seen = [ready.get(timeout=20) for _ in range(75)]
            self.assertEqual(seen.count("company"), 45)
            self.assertEqual(seen.count("laboratory"), 30)
            limiter = ProviderLimits(self.config)
            snap = limiter.snapshot()
            self.assertEqual(snap["providers"]["company"]["active"], 45)
            self.assertEqual(snap["providers"]["laboratory"]["active"], 30)
            acquired = []
            cancel = threading.Event()

            def blocked(name):
                try:
                    acquired.append(limiter.acquire("http://" + name, cancel.is_set))
                except Cancelled:
                    pass

            waiting = [threading.Thread(target=blocked, args=(n,)) for n in ["company", "laboratory"]]
            for t in waiting:
                t.start()
            time.sleep(0.5)
            self.assertEqual(acquired, [])
            cancel.set()
            for t in waiting:
                t.join(3)
                self.assertFalse(t.is_alive())
        finally:
            release.set()
            for p in workers:
                p.join(5)
            for p in workers:
                if p.is_alive():
                    p.terminate()
                    p.join()
        self.assertEqual(limiter.snapshot()["providers"]["company"]["active"], 0)
        self.assertEqual(limiter.snapshot()["providers"]["laboratory"]["active"], 0)

    def test_process_exit_releases_slot(self):
        ctx = mp.get_context("spawn")
        ready = ctx.Queue()
        p = ctx.Process(target=one_worker, args=(str(self.config), ready))
        p.start()
        ready.get(timeout=5)
        self.assertEqual(ProviderLimits(self.config).snapshot()["providers"]["company"]["active"], 1)
        p.terminate()
        p.join(3)
        self.assertEqual(ProviderLimits(self.config).snapshot()["providers"]["company"]["active"], 0)

    def test_unknown_origin_rejected_and_release_idempotent(self):
        limiter = ProviderLimits(self.config)
        with self.assertRaises(ValueError):
            limiter.acquire("http://unconfigured")
        lease = limiter.acquire("http://company")
        lease.close()
        lease.close()
        self.assertEqual(limiter.snapshot()["providers"]["company"]["active"], 0)

    def test_existing_reservation_counts_toward_provider_limit(self):
        d = json.loads(self.config.read_text())
        d["unconfirmed_previous_request_reserve"] = 45
        self.config.write_text(json.dumps(d))
        limiter = ProviderLimits(self.config)
        self.assertEqual(limiter.snapshot()["providers"]["company"]["available_for_new"], 0)
        self.assertEqual(limiter.snapshot()["providers"]["laboratory"]["available_for_new"], 0)

    def test_selfpaid_100_slots_and_disabled_policy(self):
        d = json.loads(self.config.read_text())
        d["providers"]["selfpaid"] = {"name": "selfpaid", "origin": "https://selfpaid.example", "max_active": 100}
        self.config.write_text(json.dumps(d))
        limiter = ProviderLimits(self.config)
        leases = [limiter.acquire("https://selfpaid.example/v1/responses") for _ in range(100)]
        try:
            self.assertEqual(limiter.snapshot()["providers"]["selfpaid"]["active"], 100)
            with self.assertRaises(Cancelled):
                limiter.acquire("https://selfpaid.example", lambda: True)
        finally:
            for lease in leases:
                lease.close()
        d["providers"]["selfpaid"]["enabled"] = False
        self.config.write_text(json.dumps(d))
        from opencollab_eval.commands.provider_limits import ProviderDisabled

        with self.assertRaises(ProviderDisabled):
            limiter.acquire("https://selfpaid.example")
        self.assertEqual(limiter.snapshot()["providers"]["selfpaid"]["available_for_new"], 0)

    def test_metadata_write_failure_releases_the_reserved_descriptor(self):
        from unittest.mock import patch

        limiter = ProviderLimits(self.config)
        with patch("opencollab_eval.commands.provider_limits.os.write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                limiter.acquire("http://company")
        self.assertEqual(limiter.snapshot()["providers"]["company"]["active"], 0)

    def test_response_close_releases_slot_even_if_transport_close_fails(self):
        from opencollab_eval.commands.provider_limits import LeasedResponse

        limiter = ProviderLimits(self.config)
        lease = limiter.acquire("http://company")

        class Response:
            def close(self):
                raise OSError("transport close failed")

        wrapped = LeasedResponse(Response(), lease.close)
        with self.assertRaises(OSError):
            wrapped.close()
        self.assertEqual(limiter.snapshot()["providers"]["company"]["active"], 0)

    def test_shared_limits_cover_proxy_mode_and_error_response_lifetime(self):
        import io
        import urllib.error
        from types import SimpleNamespace

        from opencollab_eval.commands.provider_limits import install

        def opened(*args, **kwargs):
            raise urllib.error.HTTPError("http://company", 429, "busy", {}, io.BytesIO(b"busy"))

        module = SimpleNamespace(
            _open_direct_upstream=opened,
            _open_default_upstream=opened,
            _client_disconnected=lambda client: False,
            _ClientDisconnected=RuntimeError,
        )
        limiter = install(module, self.config)
        self.assertIs(install(module, self.config), limiter)
        for opener in (module._open_direct_upstream, module._open_default_upstream):
            response = opener(SimpleNamespace(full_url="http://company/v1/responses"), None, 10)
            self.assertEqual(limiter.snapshot()["providers"]["company"]["active"], 1)
            self.assertEqual(response.read(), b"busy")
            response.close()
            self.assertEqual(limiter.snapshot()["providers"]["company"]["active"], 0)

    def test_invalid_policy_cannot_expand_capacity_or_enable_a_disabled_provider(self):
        original = json.loads(self.config.read_text())
        invalid = json.loads(json.dumps(original))
        invalid["unconfirmed_previous_request_reserve"] = -1
        self.config.write_text(json.dumps(invalid))
        with self.assertRaises(ValueError):
            ProviderLimits(self.config).config()
        invalid = json.loads(json.dumps(original))
        invalid["providers"]["company"]["enabled"] = 0
        self.config.write_text(json.dumps(invalid))
        with self.assertRaises(ValueError):
            ProviderLimits(self.config).config()


if __name__ == "__main__":
    unittest.main(verbosity=2)
