"""User labels and layout offsets, kept apart from reviewed source graphs."""
from copy import deepcopy
import hashlib
import json
import math


def fingerprint(graph):
    return hashlib.sha256(json.dumps(graph, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def customization(article):
    graph = article.get('map')
    saved = article.get('map_customizations') or {}
    value = saved.get(fingerprint(graph), {}) if isinstance(saved, dict) else {}
    return clean(graph, value)


def clean(graph, value):
    if not isinstance(value, dict):
        return {}
    nodes = {node['id']: node for node in (graph or {}).get('nodes', [])}
    labels = value.get('labels')
    offsets = value.get('positions')
    result = {}
    if isinstance(labels, dict):
        entries = {key: label.strip() for key, label in labels.items() if key in nodes
                   and isinstance(label, str) and 0 < len(label.strip()) <= 96
                   and label.strip() != nodes[key]['label']}
        if entries:
            result['labels'] = entries
    if isinstance(offsets, dict):
        entries = {key: list(offset) for key, offset in offsets.items()
                   if key in nodes and valid_position(offset) and any(offset)}
        if entries:
            result['positions'] = entries
    return result


def positions(article):
    ids = {node['id'] for node in (article.get('map') or {}).get('nodes', [])}
    values = customization(article).get('positions') or {}
    if not isinstance(values, dict):
        return {}
    return {key: value for key, value in values.items() if key in ids and valid_position(value)}


def valid_position(value):
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and all(type(v) in (int, float) and math.isfinite(v) and abs(v) <= 100_000 for v in value))


def display_graph(article):
    graph = deepcopy(article.get('map') or {})
    labels = customization(article).get('labels') or {}
    if not isinstance(labels, dict):
        return graph
    for node in graph.get('nodes', []):
        label = labels.get(node['id'])
        if isinstance(label, str) and 0 < len(label.strip()) <= 96 and label.strip() != node['label']:
            node['original_label'] = node['label']
            node['label'] = label.strip()
            node['manual_label'] = True
    return graph


def save(store, expected, *, node_id=None, label=None, position=None, reset_layout=False, replacement=None):
    """Change only the matching graph's overrides on the latest article row."""
    graph = expected.get('map')
    if not graph:
        raise ValueError('请先生成导图。')
    key = fingerprint(graph)
    with store.lock:
        latest = store.article(expected['id'])
        if latest['version'] != expected['version'] or fingerprint(latest.get('map')) != key or latest.get('superseded_by'):
            raise ValueError('原文或导图已更新，请重新打开后整理。')
        saved = latest.get('map_customizations')
        if not isinstance(saved, dict):
            saved = {}
        latest['map_customizations'] = saved
        before = customization(latest)
        value = deepcopy(before)
        if replacement is not None:
            if before != customization(expected):
                raise ValueError('导图已在其他页面调整，请重新打开后继续。')
            value = clean(graph, replacement)
        elif reset_layout:
            value.pop('positions', None)
        else:
            nodes = {node['id']: node for node in graph['nodes']}
            if node_id not in nodes:
                raise ValueError('这个节点已不存在，请重新选择。')
            if label is not None:
                if not isinstance(label, str) or not 0 < len(label.strip()) <= 96:
                    raise ValueError('节点文字请填写1至96字。')
                labels = value.setdefault('labels', {})
                if label.strip() == nodes[node_id]['label']:
                    labels.pop(node_id, None)
                else:
                    labels[node_id] = label.strip()
            if position is not None:
                if not valid_position(position):
                    raise ValueError('节点位置超出画布范围，请缩小移动距离。')
                value.setdefault('positions', {})[node_id] = list(position)
        value = clean(graph, value)
        if value:
            saved[key] = value
        else:
            saved.pop(key, None)
        if not saved:
            latest.pop('map_customizations', None)
        store.update_article(latest)
        return latest, before, value
