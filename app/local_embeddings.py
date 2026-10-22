"""Offline E5 inference with explicit passage/query prefixes and mean pooling."""
from functools import lru_cache
import os
import re
import threading

from .paths import resource_root

MODEL_ID = 'multilingual-e5-small:761b726d:int8:mean-l2:ws-v2'


class LocalEmbeddings:
    def __init__(self):
        self.folder = resource_root() / 'models' / 'multilingual-e5-small'
        self.lock = threading.RLock()
        self.session = None

    def available(self):
        return all((self.folder / name).is_file() for name in ('model_quantized.onnx', 'tokenizer.json'))

    def _load(self):
        if self.session is not None:
            return
        if not self.available():
            raise ValueError('本地语义模型缺失，请修复安装后重新建立索引。')
        import onnxruntime as ort
        from tokenizers import Tokenizer
        options = ort.SessionOptions()
        options.intra_op_num_threads = min(2, os.cpu_count() or 1)
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        self.tokenizer = Tokenizer.from_file(str(self.folder/'tokenizer.json'))
        self.tokenizer.enable_padding(pad_id=1, pad_token='<pad>')
        self.session = ort.InferenceSession(str(self.folder/'model_quantized.onnx'),
                                           sess_options=options, providers=['CPUExecutionProvider'])

    def encode(self, texts, *, query=False):
        import numpy as np
        with self.lock:
            self._load()
            encoded = self.tokenizer.encode_batch([('query: ' if query else 'passage: ')+re.sub(r'\s+', ' ', t).strip() for t in texts])
            if any(len(e.ids) > 512 for e in encoded):
                raise ValueError('语义检索文本过长，请缩短问题或拆分段落。')
            feed = {'input_ids': np.asarray([e.ids for e in encoded], dtype=np.int64),
                    'attention_mask': np.asarray([e.attention_mask for e in encoded], dtype=np.int64)}
            if any(i.name == 'token_type_ids' for i in self.session.get_inputs()):
                feed['token_type_ids'] = np.zeros_like(feed['input_ids'])
            output = self.session.run(None, feed)[0]
            mask = feed['attention_mask'][..., None]
            vectors = (output*mask).sum(axis=1)/np.maximum(mask.sum(axis=1), 1)
            vectors = vectors/np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
            if not np.isfinite(vectors).all():
                raise ValueError('语义模型返回无效向量。')
            return vectors.astype('<f4')

    def token_count(self, text):
        with self.lock:
            self._load()
            return len(self.tokenizer.encode('passage: '+re.sub(r'\s+', ' ', text).strip()).ids)

    @lru_cache(maxsize=64)
    def query(self, text):
        # Bound the query while retaining its beginning and end; documents are never truncated.
        text = text if len(text) <= 280 else text[:200]+' '+text[-80:]
        return self.encode([text], query=True)[0]
