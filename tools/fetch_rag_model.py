"""Download the pinned, MIT-licensed multilingual E5 ONNX conversion."""
import hashlib
import json
from pathlib import Path
import urllib.request
import subprocess
import shutil
import os

REVISION = '761b726dd34fb83930e26aab4e9ac3899aa1fa78'
FILES = {
    'onnx/model_quantized.onnx': 'f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193',
    'tokenizer.json': '0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39',
    'LICENSE': '904dc4d8749877f1dba1cda48200d2462dccbeb7c134d5e4ef6fa75e0198c8fe',
}


def main():
    folder = Path(__file__).resolve().parents[1] / 'models' / 'multilingual-e5-small'
    folder.mkdir(parents=True, exist_ok=True)
    for name, expected in FILES.items():
        target = folder / Path(name).name
        def verified(path):
            if not path.exists():
                return False
            with path.open('rb') as source:
                return hashlib.file_digest(source, 'sha256').hexdigest() == expected
        if verified(target):
            print('Verified cached '+name, flush=True)
            continue
        partial = target.with_suffix(target.suffix+'.part')
        if verified(partial):
            partial.replace(target)
            print('Verified downloaded '+name, flush=True)
            continue
        url = f'https://huggingface.co/Xenova/multilingual-e5-small/resolve/{REVISION}/{name}'
        if name == 'LICENSE':
            url = 'https://raw.githubusercontent.com/microsoft/unilm/master/LICENSE'
        curl = shutil.which('curl.exe') or shutil.which('curl')
        if curl:
            proxy = urllib.request.getproxies().get('https')
            routing = ['--proxy', proxy] if proxy else []
            if os.environ.get('RAG_DOWNLOAD_NO_PROXY'):
                routing += ['--noproxy', os.environ['RAG_DOWNLOAD_NO_PROXY']]
            subprocess.run([curl, *routing, '--fail', '--location', '--retry', '3', '--connect-timeout', '30',
                            '--max-time', '300', '--output', str(partial), url], check=True)
        else:
            with urllib.request.urlopen(url, timeout=90) as response, partial.open('wb') as output:
                while chunk := response.read(1024*1024):
                    output.write(chunk)
        with partial.open('rb') as source:
            actual = hashlib.file_digest(source, 'sha256').hexdigest()
        if actual != expected:
            raise RuntimeError('Model checksum mismatch: '+name)
        partial.replace(target)
        print('Downloaded and verified '+name, flush=True)
    (folder/'manifest.json').write_text(json.dumps({'repository': 'Xenova/multilingual-e5-small',
        'original': 'intfloat/multilingual-e5-small', 'revision': REVISION,
        'license': 'MIT', 'sha256': FILES}, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
