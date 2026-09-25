FROM python:3.11-slim
RUN pip install --no-cache-dir numpy pandas sympy pytest
RUN useradd -m runner
USER runner
WORKDIR /work
