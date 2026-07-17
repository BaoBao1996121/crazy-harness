FROM python:3.13-slim
WORKDIR /app
COPY app.py /app/app.py
RUN pip install fastapi uvicorn
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
