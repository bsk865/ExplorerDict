"""Bound workspace conversations and create local, non-generative handoffs."""
import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit

SOURCE = 'https://api-docs.deepseek.com/quick_start/pricing/'
FLASH_NAMES = {'deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp'}
DEFAULT = 16384
MAX_BUDGET = 1000000
OUTPUT = 4096


def policy(store):
    c = store.model_config()
    signature = hashlib.sha256((c['base_url'] + c['model']).encode()).hexdigest()
    official = urlsplit(c['base_url']).hostname == 'api.deepseek.com' and c['model'] in FLASH_NAMES | {'deepseek-v4-pro'}
    capacity = 1000000 if official else store.get('settings', 'context_limits', {}).get(signature)
    ceiling = min(capacity, MAX_BUDGET) if capacity else DEFAULT
    budget = store.get('settings', 'conversation_budgets', {}).get(signature, ceiling)
    budget = min(budget, ceiling)
    return {'budget':budget, 'capacity':capacity, 'official':official,
            'source':SOURCE if official else None, 'checked':'2026-10-20' if official else None,
            'ceiling':ceiling, 'output_reserve':min(OUTPUT, budget//4), 'signature':signature}


def save_policy(store, budget):
    p = policy(store)
    ceiling = p['ceiling']
    if type(budget) is not int or not 8192 <= budget <= ceiling:
        raise ValueError('问答预算须至少 8192 tokens，且不能超过已确认的模型容量。')
    values = store.get('settings', 'conversation_budgets', {})
    values[p['signature']] = budget
    store.put('settings', 'conversation_budgets', values)
    return policy(store)


def input_bound(messages):
    # Conservative text byte bound, not an exact model tokenizer. Images use a
    # generous reserve for the workspace's at-most-1800px submitted thumbnails.
    count = 256
    for m in messages:
        count += 64
        content = m.get('content', '')
        if isinstance(content, str):
            count += len(content.encode('utf-8'))
        else:
            for part in content:
                count += 16384 if part.get('type') == 'image_url' else len(str(part.get('text', '')).encode('utf-8'))
    return count


def history(session):
    rows = [{'role':m['role'], 'content':m['content']} for m in session.get('messages', [])]
    if session.get('carryover'):
        rows.insert(0, {'role':'user', 'content':'上一段会话交接摘录（仅供参考，结论须重新核对原文）：\n'+session['carryover']})
    return rows


def retrieval_budget(store, session, question):
    p = policy(store)
    remaining = p['budget'] - p['output_reserve'] - input_bound(history(session)+[{'content':question}]) - 2048
    return max(0, min(18000, remaining // 3))


def handoff(session, question, reason):
    lines = ['# 对话回顾', '', '保留本段问答与参考来源，方便继续阅读。',
             '', '## 结束原因', reason, '', '## 待继续的问题', question or '暂无待继续的问题。', '', '## 问答记录']
    for m in session.get('messages', []):
        lines += ['', '### '+('我的问题' if m['role']=='user' else '小探回答'), m['content']]
        for e in m.get('evidence', []):
            lines += ['', f"来源：{e.get('article_title','')}",
                      '> '+e.get('quote','').replace('\n','\n> ')]
    if session.get('carryover'):
        lines += ['', '## 上段回顾', session['carryover']]
    return '\n'.join(lines)


class ConversationEnded(Exception):
    def __init__(self, session):
        self.session = session


def close(store, key, question, reason):
    with store.lock:
        s = store.get('sessions', key)
        if key.startswith('library:') and s is None:
            raise ValueError('资料或对话已删除，未写回交接记录。')
        if not key.startswith('library:'):
            store.article(key.split(':')[0])
        s = s or {'messages':[], 'draft':''}
        if not s.get('closed'):
            s.update(closed=True, ended_at=datetime.now(timezone.utc).isoformat(), draft=question,
                     handoff=handoff(s, question, reason), end_reason=reason)
            store.put('sessions', key, s)
        return s


def guard(store, key, question, messages, p, cancel=None):
    if cancel and cancel.is_set():
        raise ValueError('任务已停止。')
    s = store.get('sessions', key, {})
    if s.get('closed'):
        raise ConversationEnded(s)
    if input_bound(messages) + p['output_reserve'] > p['budget']:
        has_images = any(isinstance(m.get('content'), list) and any(part.get('type') == 'image_url' for part in m['content']) for m in messages)
        if has_images:
            without_images = [dict(m, content=[part for part in m['content'] if part.get('type') != 'image_url']) if isinstance(m.get('content'), list) else m for m in messages]
            if input_bound(without_images) + p['output_reserve'] <= p['budget']:
                needed = ((input_bound(messages) + p['output_reserve'] + 1023)//1024)*1024
                suggestion = f'请将问答预算提高到至少 {needed:,} tokens，或减少图片。' if p.get('capacity') and needed <= p['capacity'] else '请减少图片，或确认模型容量后提高问答预算。'
                raise ValueError('图片预留超过本段预算，对话已保留且未发送请求。'+suggestion)
        reason = '本段对话已达到设置的长度上限，可以接续新对话。'
        raise ConversationEnded(close(store, key, question, reason))


def chat(workspace, client, key, question, messages, job):
    p = job.setdefault('conversation_policy', policy(workspace.store))
    guard(workspace.store, key, question, messages, p, job['cancel'])
    client.chat_output_limit = p['output_reserve']
    return client.chat(messages)


def finish(store, key, session, job):
    p = job.get('conversation_policy') or policy(store)
    total = (session.get('context_usage') or {}).get('total_tokens')
    session['conversation_policy'] = p
    store.put('sessions', key, session)
    if (isinstance(total, int) and total >= p['budget']) or input_bound(history(session))+p['output_reserve'] >= p['budget']:
        return close(store, key, '', '当前会话已达到问答预算，最后一条回答已保存。')
    return session


def restart(store, key):
    with store.lock:
        old = store.get('sessions', key, {})
        if not old.get('closed'):
            raise ValueError('当前对话尚未结束。')
        archived = {k:v for k,v in old.items() if k != 'archives'}
        # Keep all old messages/evidence locally; carry only a bounded excerpt.
        excerpt = '\n'.join(m['content'] for m in old.get('messages', [])[-2:])[:1500]
        s = {'messages':[], 'draft':old.get('draft',''), 'carryover':excerpt,
             'archives':old.get('archives', [])+[archived], 'article_ids':old.get('article_ids', [])}
        store.put('sessions', key, s)
        return s
