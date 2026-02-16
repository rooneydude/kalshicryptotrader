FROM python:3.13-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create logs directory
RUN mkdir -p logs paper_results

# Run as non-root user
RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser

# Dashboard port (Railway sets PORT env var automatically)
EXPOSE 8080

# Entry point
CMD ["python", "-u", "main.py"]
