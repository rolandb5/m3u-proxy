FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY m3u_proxy.py .

EXPOSE 8765

# Use uvicorn directly for better control
CMD ["python", "m3u_proxy.py"]
