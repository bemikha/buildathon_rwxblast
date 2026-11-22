# Same base image the tests were labeled on.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The labelling environment (from the kit's requirements.lock) so a few tests can be run here.
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock

# sqlglot at the pinned commit, precompiled (including pytest's rewritten test modules),
# readable by the `nobody` user the scorer runs as.
COPY fetch_sqlglot.py /tmp/fetch_sqlglot.py
RUN python /tmp/fetch_sqlglot.py /opt/sqlglot \
 && cd /opt/sqlglot \
 && python -m compileall -q sqlglot tests \
 && SKIP_INTEGRATION=0 PYTHONPATH=/opt/sqlglot python -m pytest -q -p no:cacheprovider --collect-only tests > /dev/null \
 && chmod -R a+rX /opt/sqlglot

WORKDIR /app
COPY select_tests.py main.py dynverify.py index.pkl.gz cpu_est.json /app/
RUN python -m compileall -q /app && chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1 BR_SQLGLOT=/opt/sqlglot SKIP_INTEGRATION=0
CMD ["python", "/app/main.py"]
