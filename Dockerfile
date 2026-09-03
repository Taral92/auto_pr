FROM python:3.12-slim

# git is not optional: the worker clones the PR head to review it.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Non-root, because the agent runs model-chosen tool calls over untrusted
# repository content.
RUN useradd -m app && chown -R app /app
USER app

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
