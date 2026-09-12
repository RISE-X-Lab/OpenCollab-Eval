"""Importable worker functions for installed-wheel multiprocessing checks."""

import threading
import time

from opencollab_eval.commands.provider_limits import ProviderLimits


def fill_worker(config, ready, release):
    limiter = ProviderLimits(config)
    leases = []

    def hold(provider):
        lease = limiter.acquire("http://" + provider)
        leases.append(lease)
        ready.put(provider)
        release.wait(20)
        lease.close()

    threads = [threading.Thread(target=hold, args=(n,)) for n in ["company"] * 15 + ["laboratory"] * 10]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def one_worker(config, ready):
    lease = ProviderLimits(config).acquire("http://company")
    ready.put(True)
    try:
        time.sleep(60)
    finally:
        lease.close()
