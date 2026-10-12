FROM node:22-trixie-slim AS node
FROM python:3.13-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends git chromium poppler-utils tini build-essential && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
RUN git clone https://github.com/Synthwave-Solutions/hermes-agent.git /opt/hermes && cd /opt/hermes && git checkout a8082ff74de0bb7f15323ac72501044a392f4552 && rm -rf .git && python -m venv venv && venv/bin/pip install --no-cache-dir -e . python-docx python-pptx pillow reportlab
WORKDIR /opt/opendesign
RUN npm install playwright-core@1.58.2
COPY . .
RUN useradd -m -u 1000 synthpulse
ENV HERMES_BIN=/opt/hermes/venv/bin/hermes OPENDESIGN_BIND=0.0.0.0 OPENAI_BASE_URL=http://router:20128/v1 OPENAI_MODEL=synthpulse-auto
ENV PATH=/opt/hermes/venv/bin:$PATH
USER synthpulse
EXPOSE 8289
ENTRYPOINT ["/usr/bin/tini", "--", "python", "/opt/opendesign/server.py"]
