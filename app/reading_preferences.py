"""Shared reading appearance; CSS remains the source of palette colors."""
import json
import re
from contextlib import closing
from functools import lru_cache
from .db import Database
from .paths import resource_root,project_root

PALETTES={'clay','violet','parchment','blue','forest','ocean'}
KEY='ui.reading.appearance'


def normalize(value):
    value=value if isinstance(value,dict) else {}
    return {'palette':value.get('palette') if value.get('palette') in PALETTES else 'clay',
            'mode':'dark' if value.get('mode')=='dark' else 'light'}


def from_database(db):
    try:return normalize(json.loads(db.get_setting(KEY) or '{}'))
    except (ValueError,TypeError):return normalize({})


def load(directory):
    path=directory/'explorer_dict.sqlite3'
    if not path.exists():return normalize({})
    with closing(Database(path,read_only=True)) as db:return from_database(db)


def save(directory,value):
    if value.get('palette') not in PALETTES or value.get('mode') not in {'light','dark'}:
        raise ValueError('请选择有效的阅读主题。')
    value=normalize(value)
    with closing(Database(directory/'explorer_dict.sqlite3')) as db:
        db.set_setting(KEY,json.dumps(value))
    return value


@lru_cache(maxsize=12)
def colors(palette,mode):
    roots=(resource_root()/'web', project_root()/'_native_runtime'/'web', project_root()/'web')
    root=next((candidate for candidate in roots if (candidate/'index.html').is_file()),roots[0])
    if not (root/'index.html').is_file():return {}
    html=(root/'index.html').read_text(encoding='utf-8')
    values={};specificity={}
    for file in re.findall(r'href="([^"/]+\.css)"',html):
        if not (root/file).is_file():continue
        css=re.sub(r'/\*.*?\*/','',(root/file).read_text(encoding='utf-8'),flags=re.S)
        for match in re.finditer(r'([^{}]+)\{([^{}]+)\}',css):
            selector=match[1].strip()
            if not re.fullmatch(r':root(?:\[data-(?:palette|theme)=["\w-]+\])*',selector):continue
            attrs=dict(re.findall(r'data-(palette|theme)=["\']?([\w-]+)',selector))
            if attrs.get('palette',palette)!=palette or attrs.get('theme',mode)!=mode:continue
            rank=len(attrs)
            for key,value in re.findall(r'--([\w-]+)\s*:\s*(#[0-9a-fA-F]{6})\b',match[2]):
                if rank>=specificity.get(key,-1):values[key]=value;specificity[key]=rank
    return values
