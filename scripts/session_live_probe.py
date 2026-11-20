"""Opt-in isolated live ACP test. Calls the chosen model and consumes quota.

Example on the isolated server:
python scripts/session_live_probe.py --key-file /path/to/verify-data/gateway.env
Never point this at a shared production gateway without explicitly intending to.
"""
import argparse
import json
from pathlib import Path
import secrets
import time
import urllib.error
import urllib.request
import uuid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base-url', default='http://127.0.0.1:8096')
    ap.add_argument('--key-file', required=True)
    ap.add_argument('--model', default='gemini-4-hydrogen')
    ap.add_argument('--segment-bytes', type=int, default=120_000)
    ap.add_argument('--segments', type=int, default=3)
    ap.add_argument('--output', default='session-live-results.json')
    ap.add_argument('--pause', type=float, default=0)
    ap.add_argument('--timeout', type=float, default=310)
    args = ap.parse_args()
    key = next(line.split('=', 1)[1] for line in Path(args.key_file).read_text().splitlines() if line.startswith('AGY_GATEWAY_KEY='))
    session = uuid.uuid4().hex
    headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'X-ACP-Session': session}
    system = (Path(__file__).parent/'fixtures/pi_1_5_2_system.txt').read_text()
    messages = [{'role': 'system', 'content': system}]
    markers, rows = {}, []

    def call(content):
        messages.append({'role': 'user', 'content': content})
        payload = json.dumps({'model': args.model, 'messages': messages}).encode()
        request = urllib.request.Request(args.base_url+'/v1/chat/completions', data=payload, headers=headers)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=args.timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            # The response contains only synthetic prompts, never dump request headers.
            raise RuntimeError('HTTP %d: %s' % (exc.code, exc.read().decode()[:500])) from None
        answer = data['choices'][0]['message']
        messages.append(answer)
        rows.append({'turn': len(rows)+1, 'latest_text_bytes': len(content.encode()),
                     'submitted_history_bytes': sum(len(str(m.get('content', '')).encode()) for m in messages),
                     'seconds': round(time.monotonic()-started, 2), 'usage': data.get('usage'),
                     'answer': answer.get('content')})
        Path(args.output).write_text(json.dumps({'status': 'in_progress', 'turns': rows}, indent=2)+'\n')
        print(json.dumps({k:v for k,v in rows[-1].items() if k != 'answer'}), flush=True)
        if args.pause:
            time.sleep(args.pause)
        return answer.get('content') or ''

    for number in range(args.segments):
        header = 'Remember the following data for a later exact lookup. Only reply ACK now.\n'
        line = 'Ordinary synthetic context record for a harmless conversation memory benchmark.\n'
        filler = (line * (args.segment_bytes // len(line) + 1))[:args.segment_bytes-len(header)-200]
        for offset in (0, len(filler)//2, len(filler)-100):
            name = 'S%d_P%d' % (number, offset)
            value = secrets.token_hex(8)
            markers[name] = value
            insert = '\n%s=%s\n' % (name, value)
            filler = filler[:offset] + insert + filler[offset+len(insert):]
        call(header + filler)
    answer = call('Return a JSON object with the exact values of these keys from all earlier turns: ' + ', '.join(markers) + '. Use MISSING for any value you cannot see. Output only JSON.')
    try:
        found = json.loads(answer[answer.index('{'):answer.rindex('}')+1])
    except (ValueError, TypeError):
        found = {}
    correct = [key for key,value in markers.items() if found.get(key) == value]
    with urllib.request.urlopen(urllib.request.Request(args.base_url+'/admin/status', headers=headers), timeout=30) as response:
        cache = json.load(response).get('session_cache')
    report = {'model': args.model, 'segments': args.segments, 'turns': rows, 'correct': correct,
              'missing': [key for key in markers if key not in correct], 'cache': cache,
              'all_markers_recalled': len(correct) == len(markers)}
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k != 'turns'}), flush=True)
    if report['missing']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
