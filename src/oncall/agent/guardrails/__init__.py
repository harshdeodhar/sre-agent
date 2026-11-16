"""The agent's write actions and the gate every one of them passes through.

- `policy.py`: what the agent may change and how risky each change is. Every
  name and number that limits a write is there, and nowhere else.
- `actions.py`: the four write actions. Each one works out, from typed
  parameters and a read of the cluster, exactly what it would change. None of
  them can execute anything.
- `gate.py`: the one place a change is made. It checks, dry-runs, asks for
  approval where the policy mode requires it, executes, and verifies.
- `record.py`: what is recorded about each attempted write, whatever happened.
"""
