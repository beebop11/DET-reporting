FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Run gunicorn, binding to the PORT environment variable provided by Render
CMD gunicorn wsgi:app --bind 0.0.0.0:$PORT
