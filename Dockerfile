FROM python:3.12-slim AS base


# Browsers go to a fixed, readable location rather than a user's HOME, so the
# path is the same whoever the container runs as.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    HOME=/home/app

WORKDIR /app


# The API and its Chromium process run unprivileged. The fixed UID makes volume
# ownership predictable; the image never runs as root after this point.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app


# No compiler toolchain: every dependency in requirements.txt installs from a
# wheel (psycopg2-binary bundles libpq). No Xvfb/Xauth: the browser runs
# headless, so no X display is ever required.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# Playwright drives a real Chromium session - the one and only retrieval path
# (src/fetch/playwright_client.py). Baking the browser into the image means a
# container never downloads one at runtime: a fetch either works on a cold
# start or the image is wrong, instead of failing the first sync of every new
# container. --with-deps pulls the system libraries Chromium needs; headless
# means no X server and no DISPLAY.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Patchright is still installed by requirements.txt for the retired fetch path
# behind FETCH_BACKEND=legacy, which exists only until the live check against a
# real source confirms the port. Its browser is deliberately NOT installed: the
# legacy path is not meant to run in a container.


COPY --chown=app:app src/ src/
COPY --chown=app:app sources/ sources/
COPY --chown=app:app schema/ schema/
COPY --chown=app:app migrations/ migrations/
COPY --chown=app:app prompts/ prompts/
COPY --chown=app:app data/gold/requirements_gold.json data/gold/requirements_gold.json
COPY --chown=app:app data/gold/skilled/skilled_189_real.json data/gold/skilled/skilled_190_real.json data/gold/skilled/
COPY --chown=app:app data/real_validation/skilled_189_real_chunks.json data/real_validation/skilled_190_real_chunks.json data/real_validation/
COPY --chown=app:app alembic.ini .

# The raw-snapshot volume is mounted here. Docker seeds a new named volume from
# this image directory, so its ownership is already app:app on first use.
RUN mkdir -p /app/raw /app/applicant_documents \
    && chown -R app:app /app/raw /app/applicant_documents

USER app

EXPOSE 8000

# Readiness, not mere liveness: an API with no database cannot serve real
# traffic, and /ready runs `SELECT 1`. Compose therefore will not mark the API
# healthy (or start the UI) while the database is unreachable.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "src.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
