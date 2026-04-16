FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
# build-essential and libpq-dev are required for the production-safe 'psycopg2' package
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Run the app unbuffered to ensure logs stream directly to Docker/Systemd
ENV PYTHONUNBUFFERED=1

CMD ["python", "app.py"]