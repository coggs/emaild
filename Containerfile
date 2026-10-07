FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --create-home --uid 10001 emaild && mkdir -p /data/blobs && chown -R emaild /data
COPY --from=build /install /usr/local
COPY db /app/db
WORKDIR /app
USER emaild
ENV EMAILD_DB_DIR=/app/db
ENTRYPOINT ["emaild"]
CMD ["api"]
