"""The on-call agent: a tool-use loop over the Anthropic Messages API.

This package never imports the incident library (`oncall.chaos.incident`,
`steps`, `check`) and never opens anything under `incidents/`. The agent
knows what an on-call engineer would know: the system's architecture, its
telemetry, and the alert that paged it. tests/agent/test_isolation.py checks
the imports.
"""
