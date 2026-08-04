FROM python:3.12-slim
RUN useradd -m app
USER app
COPY src /app/src
CMD ["python", "-m", "src"]
