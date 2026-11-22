FROM python:3.12-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    IN_DOCKER=1

# Install system dependencies (ffmpeg is essential for audio decoding in faster-whisper/PyAV)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY requirements.txt /app/
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Copy application source code
COPY . /app/

# Ensure directories for voice audio and models exist
RUN mkdir -p /app/voice_audio

# Default exposed port for Trello webhook
EXPOSE 8080

CMD ["python", "bot.py"]
