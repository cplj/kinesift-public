FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_INDEX=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/kinesift
COPY pyproject.toml build_backend.py README.md LICENSE NOTICE __init__.py cli.py api.py service.py environment.py oracle.py run.py contract.json observe.py sensor.py harbor.py ./
RUN --network=none python -m pip install --no-index --no-cache-dir --no-compile .

USER 65532:65532
EXPOSE 8765
CMD ["python", "-u", "-c", "from http.server import HTTPServer; from kinesift.api import Handler; HTTPServer(('0.0.0.0', 8765), Handler).serve_forever()"]
