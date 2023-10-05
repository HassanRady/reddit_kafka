FROM python:3.10

WORKDIR /main

COPY src ./src

COPY requirements.txt .

RUN pip install -r requirements.txt

CMD ["uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8080"]
