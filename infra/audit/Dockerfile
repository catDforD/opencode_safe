FROM docker-0.unsee.tech/tiangolo/uvicorn-gunicorn-fastapi:python3.11
WORKDIR /app
COPY audit_server.py /app/audit_server.py
EXPOSE 8081
CMD ["python", "/app/audit_server.py"]
