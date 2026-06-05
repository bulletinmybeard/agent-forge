# AgentForge — runtime image (image name: agentforge)
#
# Build:  docker build -t agentforge .
# Run:    docker run --rm \
#           -v "$PWD/config.yaml:/app/config.yaml:ro" \
#           agentforge "what is 2+2?"
#
# config.yaml is mounted at run time (it holds credentials) — never baked in.
FROM python:3.12-slim

WORKDIR /app

# Install the package. Add extras as needed, e.g., ".[browser,bedrock]".
COPY pyproject.toml README.md ./
COPY agentforge ./agentforge
RUN pip install --no-cache-dir .

# Ship the non-secret runtime config next to the app. config.yaml is provided
# at run time via a bind mount.
COPY tool_routing.yaml ./tool_routing.yaml
COPY profiles ./profiles
COPY config.example.yaml ./config.example.yaml

ENTRYPOINT ["agentforge"]
CMD ["--help"]
