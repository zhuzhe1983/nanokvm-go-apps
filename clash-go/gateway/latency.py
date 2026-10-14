"""On-demand HTTPS latency probes, bounded to one visible page and two workers."""
from concurrent.futures import ThreadPoolExecutor
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TEST_URL = 'https://www.gstatic.com/generate_204'
TIMEOUT_MS = 8000
CACHE_SECONDS = 60


def probe(api_key, provider, name):
    path = '/providers/proxies/' + urllib.parse.quote(provider, safe='') + '/' + urllib.parse.quote(name, safe='') + '/healthcheck?'
    path += urllib.parse.urlencode({'url': TEST_URL, 'timeout': TIMEOUT_MS})
    req = urllib.request.Request('http://127.0.0.1:9090' + path, headers={'Authorization': 'Bearer ' + api_key})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=TIMEOUT_MS / 1000 + 2) as response:
            value = json.loads(response.read(4096)).get('delay')
        if type(value) is int and 0 < value <= 65535:
            return {'state': 'ok', 'ms': value}
    except urllib.error.HTTPError as error:
        return {'state': 'timeout' if error.code in (408, 504) else 'failed', 'ms': None}
    except (TimeoutError, socket.timeout):
        return {'state': 'timeout', 'ms': None}
    except urllib.error.URLError as error:
        return {'state': 'timeout' if isinstance(error.reason, TimeoutError) else 'failed', 'ms': None}
    except (OSError, ValueError, AttributeError):
        pass
    return {'state': 'failed', 'ms': None}


class Latency:
    def __init__(self, probe_fn):
        self.probe = probe_fn
        self.lock = threading.Lock()
        self.results = {}
        self.generation = 0
        self.busy = False
        self.worker = None

    def reset(self):
        with self.lock:
            self.generation += 1
            self.results.clear()
            # An in-flight batch retains its slot until it exits. Reset cannot
            # create extra workers or let old account results overwrite new ones.

    def snapshot(self, names):
        with self.lock:
            return {name: dict(self.results.get(name, {'state': 'untested', 'ms': None})) for name in names}, self.busy

    def request(self, names, provider, force=False):
        with self.lock:
            if self.busy:
                return False
            now = time.time()
            names = [name for name in dict.fromkeys(names) if force or
                     now - self.results.get(name, {}).get('sampled_at', 0) >= CACHE_SECONDS]
            if not names:
                return False
            generation = self.generation
            self.busy = True
            for name in names:
                self.results[name] = {'state': 'pending', 'ms': None}
            self.worker = threading.Thread(target=self._batch, args=(names, provider, generation), daemon=True)
            self.worker.start()
            return True

    def _batch(self, names, provider, generation):
        def measure(name):
            with self.lock:
                if generation != self.generation:
                    return
            try:
                result = self.probe(provider, name)
            except Exception:
                result = {'state': 'failed', 'ms': None}
            with self.lock:
                if generation == self.generation:
                    self.results[name] = {**result, 'sampled_at': int(time.time())}
        try:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix='latency') as pool:
                list(pool.map(measure, names))
        finally:
            with self.lock:
                self.busy = False
