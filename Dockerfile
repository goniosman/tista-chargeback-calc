# Use official Playwright Jammy image with all Linux browser dependencies pre-installed
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Set working directory
WORKDIR /app

# Set environment defaults
ENV PYTHONUNBUFFERED=1
ENV PORT=8765
ENV HEADLESS=1

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser
RUN playwright install chromium

# Copy application source code
COPY . .

# Expose server port (Render dynamically overrides with $PORT)
EXPOSE 8765

# Start calculator server
CMD ["python", "tista_delay_fetch.py", "--serve"]
