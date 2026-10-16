import argparse
import copy
import hashlib
import json
import time
from pathlib import Path
if __package__:
    from .environment import Environment, default_script, rule_script
    from .oracle import verify
else:
    from environment import Environment, default_script, rule_script
    from oracle import verify

HERE = Path(__file__).resolve().parent
def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def run(split):
    contract = json.loads((HERE/'contract.json').read_text())
    rows = []
    started = time.perf_counter()
    for policy_name, policy in [('default_script',default_script),('rule_script',rule_script)]:
        for seed in contract[split+'_seeds']:
            for task in contract['tasks']:
                replay = []
                for repeat in range(contract['repeats']):
                    env = Environment()
                    observation = env.reset(seed, task)
                    initial = copy.deepcopy(env.initial)
                    # The policy receives only a detached observation, not verifier/expected state.
                    action, value = policy(observation)
                    transition = env.step(action, value)
                    result = verify(initial, env.state, task, contract, env.forbidden_attempts)
                    payload = {'initial':initial,'final':copy.deepcopy(env.state),'action':[action,value],'transition':transition,'verification':result}
                    replay.append(digest(payload))
                    rows.append({'policy_kind':'SCRIPTED','policy':policy_name,'split':split,'seed':seed,
                                 'task':task['id'],'repeat':repeat,'trajectory_sha256':replay[-1],**payload})
                assert len(set(replay)) == 1, 'nondeterministic replay'
    checks = []
    for seed in contract[split+'_seeds']:
        for task in contract['tasks']:
            env = Environment()
            obs = env.reset(seed, task)
            initial = copy.deepcopy(env.initial)
            # Self-reported completion without action cannot satisfy the state oracle.
            no_op = verify(initial, env.state, task, contract, [])
            assert not no_op['accepted']
            checks.append({'check':'no_op_self_report_rejected','seed':seed,'task':task['id']})
            # Correct state plus a forbidden attempt must not be accepted.
            env.step(*rule_script(obs))
            blocked = env.step('send_message','synthetic external address')
            result = verify(initial, env.state, task, contract, env.forbidden_attempts)
            assert not blocked['ok'] and not result['accepted'] and not result['forbidden_mutations']
            checks.append({'check':'forbidden_attempt_rejected_without_side_effect','seed':seed,'task':task['id']})
            # Grader must also detect a simulated out-of-band mutation.
            env.state['payments'].append('synthetic tamper')
            result = verify(initial, env.state, task, contract, env.forbidden_attempts)
            assert 'payments' in result['forbidden_mutations']
            checks.append({'check':'forbidden_mutation_detected','seed':seed,'task':task['id']})
            # Reset removes both action history and corruption.
            env.reset(seed, task)
            assert env.state == initial and not env.audit and not env.forbidden_attempts
            checks.append({'check':'clean_reset','seed':seed,'task':task['id']})
            obs['ticket']['severity'] = 999
            assert env.state['ticket']['severity'] != 999
            checks.append({'check':'observation_copy_isolated','seed':seed,'task':task['id']})
    return {'schema':'kinesift.workflow-run.v1','data_mode':'SYNTHETIC','policy_kind':'SCRIPTED_NOT_REAL_MODELS',
            'split':split,'contract_sha256':hashlib.sha256((HERE/'contract.json').read_bytes()).hexdigest(),
            'episodes':len(rows),'contract_checks':len(checks),
            'summary':{name:{'accepted':sum(r['verification']['accepted'] for r in rows if r['policy']==name),
                             'episodes':sum(r['policy']==name for r in rows)} for name in ['default_script','rule_script']},
            'elapsed_seconds':time.perf_counter()-started,'rows':rows,'checks':checks,
            'limitations':contract['limits']}

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--split',choices=['development','heldout'],default='development')
    parser.add_argument('--output',required=True)
    args = parser.parse_args()
    result = run(args.split)
    target = Path(args.output)
    with target.open('x') as stream:
        json.dump(result,stream,indent=2)
        stream.write('\n')
    print(json.dumps({'output':str(target.resolve()),'episodes':result['episodes'],
                      'contract_checks':result['contract_checks'],'summary':result['summary']}))
