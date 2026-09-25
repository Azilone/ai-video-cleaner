FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates ffmpeg libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

# Official Linux binary from the Content Authenticity Initiative release.
ARG C2PATOOL_VERSION=0.27.22
RUN curl -fLsS "https://github.com/contentauth/c2pa-rs/releases/download/c2patool-v${C2PATOOL_VERSION}/c2patool-v${C2PATOOL_VERSION}-x86_64-unknown-linux-gnu.tar.gz" \
    | tar -xz -C /usr/local/bin --strip-components=1 c2patool/c2patool \
    && chmod +x /usr/local/bin/c2patool

WORKDIR /app
COPY requirements.txt requirements-image.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY ai-video-cleaner.py ai-image-cleaner.py web.py ./
COPY templates ./templates

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT} --workers 1 --timeout 930 web:app"]
