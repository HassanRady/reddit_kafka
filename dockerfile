FROM python:3.10

WORKDIR /src

COPY src .

COPY requirements.txt .

RUN pip install -r requirements.txt
