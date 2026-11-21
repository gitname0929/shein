FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY . /app
RUN chmod 755 /app/run.sh && chmod -R a+rX /app
USER 65534:65534
CMD ["/app/run.sh", "--help"]
