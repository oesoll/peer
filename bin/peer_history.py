"""Read a bounded tail of engine history; never scrape or persist terminal output."""
import json
from peer_transport import CodexClient


def tail_records(filename, limit=2 * 1024 * 1024):
    with open(filename, 'rb') as stream:
        stream.seek(0, 2)
        end = stream.tell()
        stream.seek(max(0, end - limit))
        lines = stream.read().splitlines()
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            continue  # a bounded tail can start inside a line
    return records


def text_content(message):
    content = message.get('content', [])
    if isinstance(content, str):
        return content
    return '\n'.join(item.get('text', '') for item in content if item.get('type') in ('text', 'output_text'))


def history(row, last=3):
    messages = []
    if row['kind'] == 'codex' and row.get('codex_socket'):
        try:
            with CodexClient(row['codex_socket']) as client:
                turns = client.call('thread/turns/list', {'threadId': row['session'], 'limit': last, 'itemsView':'full'})['data']
            for turn in reversed(turns):
                for item in turn.get('items', []):
                    if item.get('type') == 'agentMessage' and item.get('text'):
                        messages.append(item['text'])
        except Exception:
            if not row.get('transcript'):
                raise ValueError('Codex history is unavailable; resume the session and try peer read again')
    if not messages and row.get('transcript'):
        for record in tail_records(row['transcript']):
            message = record.get('message', {}) if row['kind'] == 'claude' else record.get('payload', {})
            if row['kind'] == 'claude' and record.get('type') == 'assistant':
                text = text_content(message)
            elif record.get('type') == 'response_item' and message.get('role') == 'assistant':
                text = text_content(message)
            else:
                continue
            if text:
                messages.append(text)
    messages = messages[-last:]
    return {'name':row['name'], 'source':'engine-history', 'messages':messages,
            'text':'\n\n'.join(messages)[-24000:] if messages else 'No assistant answers recorded yet.\n'}
