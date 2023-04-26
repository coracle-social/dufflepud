FROM python:3.11-slim as builder
WORKDIR /app
RUN apt update && \
    apt install -y --no-install-recommends curl libpq-dev build-essential && \
    rm -rf /var/apt/lists.d/*
RUN curl -sSL https://install.python-poetry.org | python3 -
ENV PATH="$PATH:/root/.local/bin"
COPY . .
RUN poetry install

CMD poetry run ./start