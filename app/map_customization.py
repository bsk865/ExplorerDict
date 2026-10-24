"""User labels and layout offsets, kept apart from reviewed source graphs."""
from copy import deepcopy
import hashlib
import json
import math
import uuid


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
    nodes = {node['id']: deepcopy(node) for node in (graph or {}).get('nodes', [])}
    root = (graph or {}).get('root')
    result = {}
    added = value.get('added_nodes')
    accepted = []
    for entry in added if isinstance(added, list) else []:
        if (not isinstance(entry, dict) or not user_id(entry.get('id')) or entry['id'] in nodes
                or not valid_label(entry.get('label')) or not isinstance(entry.get('parent'), str)):
            continue
        node = {'id': entry['id'], 'parent': entry['parent'], 'label': entry['label'].strip(),
                'manual_node': True, 'note': str(entry.get('note') or '')[:2000]}
        nodes[node['id']] = node
        accepted.append(deepcopy(node))
    parents = value.get('parents')
    accepted_parents = {}
    if isinstance(parents, dict):
        for key, parent in parents.items():
            if (not isinstance(parent, str) or key not in nodes or parent not in nodes
                    or key == root or parent == nodes[key].get('parent')):
                continue
            nodes[key]['parent'] = parent
            accepted_parents[key] = parent
    if not valid_tree(nodes, root):
        accepted_parents = {}
        originals = {node['id']: node for node in (graph or {}).get('nodes', [])}
        originals.update({node['id']: node for node in accepted})
        nodes = deepcopy(originals)
        accepted = [node for node in accepted if valid_path(nodes, root, node['id'])]
        nodes = {key: node for key, node in nodes.items() if key in {n['id'] for n in accepted}
                 or key in {n['id'] for n in (graph or {}).get('nodes', [])}}
    if accepted:
        result['added_nodes'] = accepted
    if accepted_parents:
        result['parents'] = accepted_parents
    removed = value.get('removed_nodes')
    if isinstance(removed, list):
        removed = descendants(nodes, {key for key in removed if isinstance(key, str) and key in nodes and key != root})
        if removed:
            result['removed_nodes'] = sorted(removed)
    labels = value.get('labels')
    offsets = value.get('positions')
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
    relations = {r['id']: r for r in (graph or {}).get('relations', [])}
    added_relations = value.get('added_relations')
    if isinstance(added_relations, list):
        accepted_relations = []
        for entry in added_relations:
            if (not isinstance(entry, dict) or not user_id(entry.get('id')) or entry['id'] in relations
                    or not valid_relation(entry, nodes)):
                continue
            relation = {key: entry[key] for key in ('id', 'src', 'dst')}
            relation.update(label=entry['label'].strip(), note=str(entry.get('note') or '')[:2000], manual_relation=True)
            accepted_relations.append(relation)
            relations[relation['id']] = relation
        if accepted_relations:
            result['added_relations'] = accepted_relations
    edits = value.get('relations')
    if isinstance(edits, dict):
        accepted_edits = {}
        for key, entry in edits.items():
            if key in relations and isinstance(entry, dict) and valid_relation(entry, nodes):
                accepted_edits[key] = {field: entry[field].strip() for field in ('src', 'dst', 'label')}
                accepted_edits[key]['note'] = str(entry.get('note') or '')[:2000]
        if accepted_edits:
            result['relations'] = accepted_edits
    removed_relations = value.get('removed_relations')
    if isinstance(removed_relations, list):
        entries = sorted({key for key in removed_relations if isinstance(key, str) and key in relations})
        if entries:
            result['removed_relations'] = entries
    return result


def user_id(value):
    return isinstance(value, str) and value.startswith('user:') and 5 < len(value) <= 80


def valid_label(value):
    return isinstance(value, str) and 0 < len(value.strip()) <= 96


def descendants(nodes, keys):
    found = set(keys)
    while True:
        expanded = found | {key for key, node in nodes.items() if node.get('parent') in found}
        if expanded == found:
            return found
        found = expanded


def valid_tree(nodes, root):
    return root in nodes and all(valid_path(nodes, root, key) for key in nodes)


def valid_path(nodes, root, current):
    seen = set()
    while current != root:
        if current not in nodes or current in seen or len(seen) >= 9:
            return False
        seen.add(current)
        current = nodes[current].get('parent')
    return True


def valid_relation(entry, nodes):
    return (isinstance(entry.get('src'), str) and isinstance(entry.get('dst'), str)
            and entry['src'] in nodes and entry['dst'] in nodes and entry['src'] != entry['dst']
            and valid_label(entry.get('label')))


def positions(article):
    ids = {node['id'] for node in display_graph(article).get('nodes', [])}
    values = customization(article).get('positions') or {}
    if not isinstance(values, dict):
        return {}
    return {key: value for key, value in values.items() if key in ids and valid_position(value)}


def valid_position(value):
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and all(type(v) in (int, float) and math.isfinite(v) and abs(v) <= 100_000 for v in value))


def display_graph(article):
    graph = deepcopy(article.get('map') or {})
    return apply_display(graph, customization(article)) if graph else {}


def apply_display(graph, value):
    graph = deepcopy(graph)
    nodes = graph.setdefault('nodes', [])
    nodes.extend(deepcopy(value.get('added_nodes', [])))
    removed = set(value.get('removed_nodes', []))
    labels = value.get('labels', {})
    for node in nodes:
        if node['id'] in value.get('parents', {}):
            node['original_parent'] = node.get('parent')
            node['parent'] = value['parents'][node['id']]
            node['manual_parent'] = True
        label = labels.get(node['id'])
        if label:
            node['original_label'] = node['label']
            node['label'] = label.strip()
            node['manual_label'] = True
    graph['nodes'] = [node for node in nodes if node['id'] not in removed]
    visible = {node['id'] for node in graph['nodes']}
    relations = graph.setdefault('relations', [])
    relations.extend(deepcopy(value.get('added_relations', [])))
    for relation in relations:
        edit = value.get('relations', {}).get(relation['id'])
        if edit:
            relation['original_relation'] = deepcopy(relation)
            relation.update(edit)
            relation['manual_relation'] = True
    graph['relations'] = [r for r in relations if r['id'] not in value.get('removed_relations', [])
                          and r['src'] in visible and r['dst'] in visible]
    for binding in graph.get('bindings', []):
        binding['node_ids'] = [key for key in binding.get('node_ids', []) if key in visible]
    return graph


def structure_change(graph, value, action, data):
    if not isinstance(data, dict):
        raise ValueError('请重新选择要整理的内容。')
    effective = apply_display(graph, value)
    nodes = {n['id']: n for n in effective.get('nodes', [])}
    root = graph['root']
    key = data.get('id')
    if action == 'reset':
        return {}
    if action == 'add_node':
        parent = data.get('parent')
        if parent not in nodes or not valid_label(data.get('label')):
            raise ValueError('请选择上级节点，并填写1至96字的节点文字。')
        node = {'id': 'user:'+uuid.uuid4().hex, 'parent': parent,
                'label': data['label'].strip(), 'note': str(data.get('note') or '')[:2000]}
        nodes[node['id']] = node
        if not valid_tree(nodes, root):
            raise ValueError('最多支持九层下级节点，请选择更靠上的节点。')
        value.setdefault('added_nodes', []).append(node)
    elif action in {'reparent', 'remove_node'}:
        if key not in nodes or key == root:
            raise ValueError('请先选择一个非中心节点。')
        if action == 'remove_node':
            removed = set(value.get('removed_nodes', [])) | descendants(nodes, {key})
            value['removed_nodes'] = sorted(removed)
        else:
            parent = data.get('parent')
            if parent not in nodes or parent in descendants(nodes, {key}):
                raise ValueError('上级节点不能是自己或自己的下级节点。')
            nodes[key]['parent'] = parent
            if not valid_tree(nodes, root):
                raise ValueError('调整后超过九层下级节点，请选择更靠上的节点。')
            original = next(n for n in graph['nodes']+value.get('added_nodes', []) if n['id'] == key)
            if parent == original.get('parent'):
                value.setdefault('parents', {}).pop(key, None)
            else:
                value.setdefault('parents', {})[key] = parent
    elif action in {'add_relation', 'edit_relation', 'remove_relation'}:
        relations = {r['id']: r for r in effective['relations']}
        if action != 'add_relation' and key not in relations:
            raise ValueError('这条关系已不存在，请重新选择。')
        if action == 'remove_relation':
            value['removed_relations'] = sorted(set(value.get('removed_relations', [])) | {key})
        else:
            if not valid_relation(data, nodes):
                raise ValueError('请选择两个不同的节点，并填写1至96字的关系文字。')
            relation = {field: data[field].strip() for field in ('src', 'dst', 'label')}
            relation['note'] = str(data.get('note') or '')[:2000]
            if action == 'add_relation':
                relation['id'] = 'user:'+uuid.uuid4().hex
                value.setdefault('added_relations', []).append(relation)
            else:
                value.setdefault('relations', {})[key] = relation
    else:
        raise ValueError('暂不支持这个整理操作。')
    return value


def save(store, expected, *, node_id=None, label=None, position=None, reset_layout=False, replacement=None, action=None, data=None):
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
        elif action:
            if before != customization(expected):
                raise ValueError('导图已在其他页面调整，请重新打开后继续。')
            value = structure_change(graph, value, action, data or {})
        elif reset_layout:
            value.pop('positions', None)
        else:
            nodes = {node['id']: node for node in apply_display(graph, value)['nodes']}
            if node_id not in nodes:
                raise ValueError('这个节点已不存在，请重新选择。')
            if label is not None:
                if not isinstance(label, str) or not 0 < len(label.strip()) <= 96:
                    raise ValueError('节点文字请填写1至96字。')
                labels = value.setdefault('labels', {})
                original = next(n for n in graph['nodes']+value.get('added_nodes', []) if n['id'] == node_id)
                if label.strip() == original['label']:
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
