# Test image for tests/linux/run.sh: CPU PyTorch and Lightning; the helpers are mounted, not copied.
FROM python:3.12-slim
RUN pip install --no-cache-dir torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir lightning==2.6.6
COPY wdtick /wdtick
RUN pip install --no-cache-dir /wdtick
