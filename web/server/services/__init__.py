"""Services dashboard — read-only ops view of agentforge infrastructure.

Probes the Docker socket (mounted into agentforge-web), pings Ollama + Redis,
and exposes a REST API consumed by ``/services`` in the SPA.
"""
