# The code sandbox: one tiny stdlib-only runner, nothing else. See
# docker-compose.yml (this service gets NO network, no secrets, no data
# volume) and docs/sandbox.md. Deliberately a separate image from Nori's.
FROM python:3.12-slim

# The unprivileged user every run executes as. No home, no shell.
RUN useradd --uid 53417 --user-group --no-create-home --shell /usr/sbin/nologin sandbox

COPY runner.py /opt/runner/runner.py

# Starts as root only so it can create run directories, chown them, drop each
# run to its own uid, and kill leftovers -- compose strips every other capability.
CMD ["python3", "-u", "/opt/runner/runner.py"]
