# WorkCRM — stdlib Python, no pip
FROM python:3.11-slim

WORKDIR /app
COPY crm.py .

EXPOSE 8765

CMD ["python3", "crm.py", "--host", "0.0.0.0", "--port", "8765"]
