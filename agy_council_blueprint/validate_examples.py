"""Validate the design packet's example contracts; this is not a council engine."""
import copy
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KINDS = {'independent', 'critique', 'revise', 'synthesize', 'audit'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check(obj):
    require(obj['schema_version'] == 1, 'Unsupported schema version')
    require(obj['draft'] is True, 'Packet examples must be marked drafts')
    require(bool(obj['name']) and bool(obj['goal']), 'Missing name/goal')
    require(obj['execution_mode'] == 'supplied_evidence', 'Unsupported example execution mode')
    lim = obj['limits']
    for name in ['global_concurrency', 'per_account_concurrency', 'max_model_calls', 'max_wall_seconds']:
        require(type(lim[name]) is int and lim[name] > 0, 'Invalid limit '+name)
    require(type(lim['max_retries_per_task']) is int and lim['max_retries_per_task'] >= 0, 'Invalid retry limit')
    require(lim['automatic_account_switching'] is False, 'Unexpected automatic switching')
    input_ids = [x['id'] for x in obj['inputs']]
    require(len(input_ids) == len(set(input_ids)) and bool(input_ids), 'Duplicate/missing input IDs')
    workers = obj['workers']
    ids = [w['id'] for w in workers]
    require(bool(ids) and len(ids) == len(set(ids)), 'Duplicate/missing worker IDs')
    for w in workers:
        for key in ['id', 'name', 'account_ref', 'model', 'role', 'instructions', 'task']:
            require(isinstance(w[key], str) and bool(w[key]), 'Missing worker field '+key)
    seen = set()
    calls = 0
    for s in obj['stages']:
        require(s['id'] not in seen, 'Duplicate stage ID')
        require(s['kind'] in KINDS, 'Invalid stage kind')
        require(s['context'] in {'fresh', 'continue'}, 'Invalid context policy')
        require(s['release'] == 'after_all_required', 'Invalid release policy')
        require(s['failure_policy'] == 'needs_attention', 'Invalid required-worker failure policy')
        require(bool(s['workers']) and len(s['workers']) == len(set(s['workers'])), 'Duplicate/missing stage participants')
        require(set(s['workers']) <= set(ids), 'Unknown stage worker')
        require(set(s['input_stages']) <= seen, 'Inputs refer to a future or missing stage')
        require(bool(s['instruction']) and bool(s['required_sections']), 'Missing stage contract')
        seen.add(s['id'])
        calls += len(s['workers'])
    require(bool(seen) and obj['final_stage'] == obj['stages'][-1]['id'], 'Invalid final stage')
    require(calls <= lim['max_model_calls'], 'Required calls exceed budget before retries')
    return {'workers': len(ids), 'stages': len(seen), 'base_model_calls': calls,
            'live_bindings_verified': False, 'configuration_checks': 'PASS'}


def main():
    results = {}
    loaded = []
    for p in sorted((ROOT/'examples').glob('*.json')):
        o = json.loads(p.read_text(encoding='utf-8'))
        results[p.name] = check(o)
        loaded.append(o)
    require(len(loaded) == 3, 'Expected three example workflows')
    negative = []
    for label, change in [
        ('duplicate_worker', lambda o: o['workers'].append(copy.deepcopy(o['workers'][0]))),
        ('missing_actor', lambda o: o['stages'][0]['workers'].append('not_a_worker')),
        ('future_input', lambda o: o['stages'][0]['input_stages'].append(o['stages'][-1]['id'])),
        ('invalid_budget', lambda o: o['limits'].update(max_model_calls=0)),
    ]:
        o = copy.deepcopy(loaded[0])
        change(o)
        try:
            check(o)
        except ValueError:
            negative.append({'case': label, 'rejected': True})
        else:
            raise AssertionError('Invalid example was accepted: '+label)
    report = {'scope': 'Design packet consistency only; no production schema/provider/application test',
              'examples': results, 'negative_cases': negative,
              'remaining': ['Account/model/input binding', 'Actual application implementation',
                            'CLI protocol and OAuth isolation tests', 'Windows process lifecycle tests']}
    (ROOT/'VALIDATION.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    manifest = {}
    for p in sorted(ROOT.rglob('*')):
        if p.is_file() and p.name != 'MANIFEST.json' and '__pycache__' not in p.parts:
            manifest[p.relative_to(ROOT).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    (ROOT/'MANIFEST.json').write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
