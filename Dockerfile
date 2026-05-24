# habeshamarketresearch.com — static-site server for Railway.
#
# Why this exists: the repo is two HTML files (index.html, login.html). With
# no Procfile / package.json / requirements.txt, Railway's Nixpacks builder
# has nothing to detect, so the service never starts and Railway returns its
# fallback 502 ("Application failed to respond"). A Dockerfile gives Railway
# an explicit, self-contained build with no buildpack ambiguity.
#
# Why python http.server: zero dependencies, runs on python:3.12-alpine
# (~50MB image), and the site is two pages behind Railway's edge cache —
# concurrency doesn't matter. Swap to nginx/caddy later if traffic grows.

FROM python:3.12-alpine

WORKDIR /site

# Copy only what we serve. Keeps the image small and avoids leaking .git
# or editor junk into the running container.
COPY index.html login.html ./

# Railway injects $PORT at runtime; 8080 is a sane fallback for local
# `docker run` so the same image works for smoke-testing.
ENV PORT=8080
EXPOSE 8080

# --bind 0.0.0.0 is critical: http.server defaults to binding all interfaces
# already in 3.12 but being explicit prevents a silent regression if the
# default ever changes. Shell form so $PORT expands at container start.
CMD python -m http.server "$PORT" --bind 0.0.0.0
