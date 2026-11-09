# Run the whole project without installing Python: see README, "Running with Docker".
#   docker compose run --rm tests     # lint + unit + sanity + regression (what CI gates on)
#   docker compose run --rm demo      # the end-to-end demo; results land in ./results
#   docker compose run --rm report    # Allure report -> ./reports/allure-report.html

# --- test: the code and its pinned dependencies -----------------------------------------------------
FROM python:3.14-slim AS test

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLBACKEND=Agg

# A non-root user with the usual first-user uid, so files written to bind mounts belong to the host user
RUN useradd --create-home --uid 1000 qa
WORKDIR /app
# The user must own /app itself, not just the copied files: tools write caches, coverage.xml and results/ here
RUN chown qa:qa /app

# Dependencies first: this layer is reused until the lock file changes
COPY requirements.txt requirements-dev.txt requirements-lock.txt ./
RUN pip install -r requirements-lock.txt

COPY --chown=qa:qa . .
USER qa

CMD ["python", "tools/check.py", "ci", "--parallel"]

# --- report: adds Java and the Allure command-line tool --------------------------------------------
FROM test AS report

USER root
ARG ALLURE_VERSION=2.30.0
RUN apt-get update \
 && apt-get install -y --no-install-recommends default-jre-headless curl unzip \
 && curl -fsSL -o /tmp/allure.zip \
      "https://repo.maven.apache.org/maven2/io/qameta/allure/allure-commandline/${ALLURE_VERSION}/allure-commandline-${ALLURE_VERSION}.zip" \
 && unzip -q /tmp/allure.zip -d /opt \
 && ln -s "/opt/allure-${ALLURE_VERSION}/bin/allure" /usr/local/bin/allure \
 && rm -rf /tmp/allure.zip /var/lib/apt/lists/*
USER qa

# Run the regression with Allure results, then build one self-contained HTML file in /app/reports.
# The report is written even if tests fail; the container's exit code is the tests'.
CMD ["sh", "-c", "python tools/check.py regression --parallel --allure allure-results; status=$?; \
allure generate allure-results -o /tmp/allure-report --clean --single-file \
&& mkdir -p reports && cp /tmp/allure-report/index.html reports/allure-report.html \
&& echo 'Report: reports/allure-report.html'; exit $status"]
