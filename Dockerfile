# Render (and most PaaS hosts) run containers on Linux, which is required
# anyway — py-tgcalls' native call-streaming engine doesn't run on bare
# Windows (see README). This image also carries ffmpeg, which Render's
# native Python runtime doesn't include but py-tgcalls needs for every
# stream.
FROM python:3.12-slim

# ffmpeg: required by py-tgcalls for voice-chat audio/video encoding.
# build-essential: fallback in case TgCrypto (a C extension) has no
# prebuilt wheel for this exact platform/Python combo and needs to compile
# from source — cheap to include, avoids an otherwise opaque build failure.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render's filesystem is ephemeral by default (no disk attached) — these
# just need to exist at boot, not persist across deploys/restarts. Attach a
# Render persistent disk at /app/cookies if you want cookies.txt to survive
# redeploys (see README's "Deploying on Render" section).
RUN mkdir -p cookies downloads

CMD ["python", "main.py"]
