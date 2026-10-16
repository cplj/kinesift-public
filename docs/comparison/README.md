Comparison report · Version 0.1.0

# Two scripts. One synthetic contract.

Under these conditions, an observation-aware rule script passed more synthetic task attempts than a constant-action script. These are inspectable deterministic scripts, with no external model or commercial system under test.

## Method and fixed acceptance rule

The distributed [task contract](https://github.com/cplj/kinesift-public/blob/main/contract.json) defines routing, priority, resolution and labeling rules, three variants per family, development seeds 11, 23, 37 and held-out seeds 71, 83, 97. Each script runs each task and seed twice from a clean reset. Each split contains 72 attempts per script, 144 in total. Repeats verify deterministic replay; they are not independent samples.

The acceptance rule requires the entire final state to equal the expected state, with zero forbidden mutations and zero forbidden attempts. Protected tickets, simulated payments and outbound messages must remain unchanged. Self-reported completion does not satisfy this rule. The rules and seed sets are fixed in the contract; this page does not establish an independently witnessed pre-execution timestamp.

The constant-action script always chooses support, normal, resolved or complete for the corresponding family. The rule script reads the observation and applies the disclosed task rules. Both receive the same detached observation and one action per attempt, use identical reset and grading logic, and run without external model calls. The constant-action script is a deliberately simple baseline, not a substitute for an optimized general agent.

## Results under these conditions

| Split | Constant-action script | Observation-aware rule script |
| --- | --- | --- |
| Development | 34 / 72 | 72 / 72 |
| Held-out seeds | 40 / 72 | 72 / 72 |

Independent local verification reproduced these values and 180 contract checks per split. Those checks cover no-op rejection, forbidden-attempt rejection, simulated out-of-band mutation detection, clean reset and detached observations. There are no excluded attempts in these denominators. No latency, full-cost or statistical-significance result is claimed.

## Limitations and unfavorable evidence

The rule script has the exact task rules in its implementation. Its full acceptance does not establish unseen-task generalization, model learning or resistance to deliberate manipulation of evaluator internals. Held-out seeds change synthetic inputs within the same known families. The constant-action baseline still succeeds on 34 and 40 attempts, so a pass alone is not evidence of sophisticated reasoning.

These results do not compare actual external evaluation products. In-process code can modify the evaluator. Omitted actions and effects outside the simulation are unobserved. Scripted success does not establish real-world safety.

## Reproduce locally

After installing from GitHub, choose new output paths; existing files are never overwritten:

```sh
python -m kinesift.run --split development --output /tmp/kinesift-development-new.json
python -m kinesift.run --split heldout --output /tmp/kinesift-heldout-new.json
```

The result includes per-attempt initial and final state, actions, verification, repeat digests and the five contract-check families. [Script implementations](https://github.com/cplj/kinesift-public/blob/main/environment.py) · [State oracle](https://github.com/cplj/kinesift-public/blob/main/oracle.py) · [Runner](https://github.com/cplj/kinesift-public/blob/main/run.py).
