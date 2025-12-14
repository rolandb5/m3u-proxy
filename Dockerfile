FROM python:3.11-slim

WORKDIR /app

COPY m3u_proxy.py .

# No dependencies needed - uses only standard library!

EXPOSE 8765

CMD ["python", "m3u_proxy.py"]

