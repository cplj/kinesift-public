"""Replace decide() with your agent; keep the observation/replay protocol."""
import json
import sys
from urllib.request import Request, urlopen


def decide(observation):
    queue = {'billing': 'finance', 'incident': 'support', 'outage': 'infra'}
    return [{'action': 'set_queue', 'value': queue[observation['ticket']['topic']]}]


def call(endpoint, request):
    body = json.dumps(request).encode('utf-8')
    with urlopen(Request(endpoint, body, {'Content-Type': 'application/json'}), timeout=10) as response:
        return json.load(response)


if __name__ == '__main__':
    endpoint = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8765/run'
    request = {'task': 'route-0', 'seed': 11}
    observation = call(endpoint, request)['observation']
    request['actions'] = decide(observation)
    result = call(endpoint, request)
    print(json.dumps(result))
    raise SystemExit(0 if result['status'] == 'SUCCEEDED' else 1)
