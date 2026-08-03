FROM python:3.11-slim-bookworm

# System deps + Google Chrome (Debian bookworm no longer ships pinned chromium packages)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libgdk-pixbuf2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libxkbcommon0 \
    libdrm2 \
    libgbm1 \
    libxshmfence1 \
    xdg-utils \
    && wget -q -O /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends /tmp/google-chrome.deb \
    && rm -f /tmp/google-chrome.deb \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/google-chrome
# Leave CHROMEDRIVER_PATH unset so webdriver-manager matches Chrome at runtime
ENV HEADLESS=True

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY database.py extraction.py script_clean.py monitor.py fintalent_monitor.py ./
COPY scripts/ ./scripts/
CMD ["python", "-u", "monitor.py"]
