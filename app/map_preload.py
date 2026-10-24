"""Bounded, silent preparation of maps for newly acquired or opened articles."""
import hashlib
import json
import threading
import time
from collections import OrderedDict

from . import article_map


def fingerprint(article, signature, mode='auto'):
    from .map_input import resolve_mode
    words = list(dict.fromkeys(o['title'] for o in article.get('objects', []) if o['type'] == 'term'))
    data = [article['version'], article['title'], article['blocks'], words,
            signature, article_map.POLICY_VERSION, 'prepared-map-v2', resolve_mode(article, mode)]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def cached(article, key):
    return bool(article.get('map') and (article.get('quality') or {}).get('approved')
                and article.get('map_input') == key)


class MapPreloader:
    def __init__(self, workspace):
        self.workspace = workspace
        self.lock = threading.Lock()
        self.pending = OrderedDict()
        self.seen = OrderedDict()
        self.stop = threading.Event()
        self.thread = None

    def offer(self, article):
        if (self.stop.is_set() or article.get('superseded_by')
                or article.get('source_status') == 'pending_source'
                or not article.get('blocks') or article.get('origin') == 'demo'):
            return
        config = self.workspace.config()
        if not config['has_key']:
            return
        signature = config.get('map_signature', config['signature'])
        key = fingerprint(article, signature)
        # Preserve existing maps made before background preparation was introduced.
        if cached(article, key) or (article.get('map') and not article.get('map_input')):
            return
        if article.get('map') and cached(article, fingerprint(article, signature, (article.get('quality') or {}).get('input_mode', 'auto'))):
            return
        now = time.monotonic()
        with self.lock:
            if self.seen.get(key, 0) > now:
                return
            self.seen[key] = now + 900
            while len(self.seen) > 256:
                self.seen.popitem(last=False)
            self.pending[article['id']] = (key, now + 1.5)
            self.pending.move_to_end(article['id'])
            while len(self.pending) > 8:
                self.pending.popitem(last=False)
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()

    def _run(self):
        while not self.stop.wait(.25):
            with self.workspace.lock:
                busy = any(j['status'] == 'running' for j in self.workspace.jobs.values())
            if busy:
                continue
            with self.lock:
                if not self.pending:
                    self.thread = None
                    return
                aid = next(reversed(self.pending))
                key, ready = self.pending[aid]
                if time.monotonic() < ready:
                    continue
                self.pending.pop(aid)
            try:
                article = self.workspace.store.article(aid)
                if (article.get('superseded_by') or article.get('source_status') == 'pending_source'
                        or key != fingerprint(article, self.workspace.config().get('map_signature', self.workspace.config()['signature']))
                        or cached(article, key)):
                    continue
                self.workspace.start('map', {'article_id': aid}, background=True)
            except Exception:
                # A failed preparation never interrupts reading or retries in a loop.
                import logging
                logging.getLogger(__name__).debug('Map preparation deferred', exc_info=True)

    def close(self):
        self.stop.set()
        with self.lock:
            self.pending.clear()
