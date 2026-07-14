# Trading bot — single image shared by 3 services (executor, signal, dashboard).
# .env is NEVER baked (excluded in .dockerignore); mounted at runtime.
FROM python:3.12-slim

WORKDIR /app

# System deps.
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash \
    && rm -rf /var/lib/apt/lists/*

# Python deps (union of all components).
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
        python-binance>=1.0.19 \
        python-dotenv>=1.0.0 \
        pyyaml>=6.0 \
        requests>=2.31.0 \
        fastapi>=0.110 \
        "uvicorn[standard]>=0.29" \
        pytest>=8.0

# Copy code (config.yaml included; .env / .venv / state excluded via .dockerignore).
COPY . /app

# Make scripts executable.
RUN chmod +x vibe-trading/generate_signal.sh

# Default: run unit tests (overridden per-service in compose).
CMD ["python", "-m", "pytest", "executor/test_risk_guard.py", "vibe-trading/test_scanner.py", "-q"]
