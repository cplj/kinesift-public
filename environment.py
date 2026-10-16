import copy
import random

class Environment:
    def reset(self, seed, task):
        rng = random.Random(seed * 101 + task['variant'] * 17 + ['route','prioritize','resolve','label'].index(task['kind']))
        ticket = {'id': 'T-' + str(rng.randrange(1000,9999)),
                  'topic': rng.choice(['billing','incident','outage']),
                  'severity': rng.randrange(1,5),
                  'checklist': [bool(rng.randrange(2)) for _ in range(3)],
                  'reply_needed': bool(rng.randrange(2)),
                  'queue': 'unassigned', 'priority': 'unset', 'status': 'open', 'label': ''}
        # Explicit variant ensures both permitted resolution outcomes occur.
        if task['kind'] == 'resolve' and task['variant'] == 0:
            ticket['checklist'] = [True, True, True]
        self.state = {'ticket': ticket, 'protected_ticket': {'id':'P-1','status':'locked'},
                      'payments': [], 'outbound_messages': []}
        self.initial = copy.deepcopy(self.state)
        self.task = copy.deepcopy(task)
        self.audit = []
        self.forbidden_attempts = []
        return self.observe()

    def observe(self):
        return {'task': copy.deepcopy(self.task), 'ticket': copy.deepcopy(self.state['ticket'])}

    def step(self, action, value):
        fields = {'set_queue': ('queue', ['finance','support','infra']),
                  'set_priority': ('priority', ['urgent','normal']),
                  'set_status': ('status', ['resolved','blocked']),
                  'set_label': ('label', ['followup','complete'])}
        if action not in fields:
            self.forbidden_attempts.append({'action':action,'value':value})
            return {'ok':False,'reason':'forbidden action'}
        field, allowed = fields[action]
        if value not in allowed:
            self.audit.append({'action':action,'value':value,'ok':False})
            return {'ok':False,'reason':'invalid value'}
        self.state['ticket'][field] = value
        self.audit.append({'action':action,'value':value,'ok':True})
        return {'ok':True}

def default_script(observation):
    return {'route':('set_queue','support'),'prioritize':('set_priority','normal'),
            'resolve':('set_status','resolved'),'label':('set_label','complete')}[observation['task']['kind']]

def rule_script(observation):
    t = observation['ticket']
    return {'route':('set_queue',{'billing':'finance','incident':'support','outage':'infra'}[t['topic']]),
            'prioritize':('set_priority','urgent' if t['severity']>=3 else 'normal'),
            'resolve':('set_status','resolved' if all(t['checklist']) else 'blocked'),
            'label':('set_label','followup' if t['reply_needed'] else 'complete')}[observation['task']['kind']]
