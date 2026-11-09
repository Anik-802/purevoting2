# Build context = repository root (see ledger/gateway/compose-snippet.yml). Standard library only, no pip install.
FROM python:3.12-slim
WORKDIR /app
COPY ledger/gateway/*.py ledger/gateway/
COPY ledger/tools/ethsign.py ledger/tools/
EXPOSE 8100
CMD ["python3", "-I", "ledger/gateway/gw_server.py", "--port", "8100", "--host", "0.0.0.0", "--db", "/data/gateway.db"]
